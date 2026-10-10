# -*- coding: utf-8 -*-
"""WorkBuddy 每日积分自动领取。

思路（来自 linux.do 帖子）：只读本地登录态文件 -> 解出 accessToken -> 调官方签到接口。
    - 只读登录态文件，不修改任何文件
    - 不打印 token
    - 结果写入同目录 checkin.log
    - 兼容 5.6.0+ 的 $wbEncrypted 信封加密登录态（AES-256-GCM 字段级加密）

凭证：
    %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\*.info
    5.6.0 之前 accessToken 为明文 JSON；5.6.0 起改为 $wbEncrypted 信封：
        {"auth": {"accessToken": {"$wbEncrypted":1, "envelope": "<base64>"}, ...}}
    密钥由构建期内嵌的 atRestSecretKey 经 SHA-256 派生（同版本通用）。

签到接口：
    POST https://copilot.tencent.com/v2/billing/meter/checkin-status
    POST https://copilot.tencent.com/v2/billing/meter/daily-checkin
    Headers：Authorization: Bearer <accessToken> + X-User-Id + X-Domain
    服务端状态码：code:0 成功，code:10001 今天已签到请明天再来
"""
import base64
import hashlib
import json
import os
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from Crypto.Cipher import AES

# ---- 配置 ----
API_BASE = "https://copilot.tencent.com/v2/billing/meter"
AUTH_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "CodeBuddyExtension" / "Data" / "Public" / "auth"

BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / "checkin.log"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ---- $wbEncrypted 信封解密 ----
# WorkBuddy 5.6.0 起登录态敏感字段改为 AES-256-GCM 加密，密钥由构建期内嵌的
# atRestSecretKey 派生（提取自 WorkBuddy 主进程，同版本通用）。
_AT_REST_SECRET = "Sik9U5aXhCdwTVEwsEySDOmDoB9r9ntFxHF1fst9LQI="
_WB_KEY = hashlib.sha256(_AT_REST_SECRET.encode("utf-8")).digest()
_WB_KEY_ID = hashlib.sha256(_WB_KEY).hexdigest()[:16]

_FRAMING_NUM = {"file": 1, "field": 2, "record": 3, "stream": 4}
_FRAMING_TAG = {"file": b"WBEF1", "field": b"WBEV1", "record": b"WBER1", "stream": b"WBES1"}


def _wb_aad(framing: str, key_id: str, suite: int) -> bytes:
    """复刻 buildAuthenticatedContextAad（sym-v1）的字节布局"""
    return b"".join([
        b"WB-AAD\x00", b"\x01",
        struct.pack(">I", len(_FRAMING_TAG[framing])) + _FRAMING_TAG[framing],
        struct.pack(">I", 6) + b"sym-v1",
        struct.pack(">I", suite),
        struct.pack(">I", len(key_id)) + key_id.encode(),
        bytes([_FRAMING_NUM[framing]]),
        b"\x00",  # sequence 未定义
        b"\x00",  # final 未定义
    ])


def open_wb_field(value):
    """解密 $wbEncrypted 字段封装；明文值原样返回。失败抛异常。"""
    if not isinstance(value, dict) or value.get("$wbEncrypted") != 1:
        return value
    if value.get("scheme") not in (None, "sym-v1"):
        raise ValueError(f"不支持的加密方案 {value.get('scheme')}，请重新登录 WorkBuddy")
    env = json.loads(base64.b64decode(value["envelope"]))
    if env.get("keyId") != _WB_KEY_ID:
        raise ValueError(f"密钥不匹配（envelope keyId={env.get('keyId')}），WorkBuddy 版本可能已更新")
    cipher = AES.new(
        _WB_KEY, AES.MODE_GCM,
        nonce=base64.b64decode(env["nonce"]),
    )
    cipher.update(_wb_aad("field", env["keyId"], env["suite"]))
    plain = cipher.decrypt_and_verify(
        base64.b64decode(env["ciphertext"]),
        base64.b64decode(env["authTag"]),
    )
    return plain.decode("utf-8")


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_accounts() -> list:
    """从 auth 目录读取登录态（解密 accessToken），按 token 去重"""
    if not AUTH_DIR.is_dir():
        log(f"ERROR: 未找到登录态目录 {AUTH_DIR}，请先打开 WorkBuddy 并登录")
        return []
    accounts, seen = [], set()
    for path in sorted(AUTH_DIR.glob("*.info")):
        name = path.stem
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
            auth = session.get("auth") or {}
            token = open_wb_field(auth.get("accessToken"))
            if not token:
                log(f"ERROR: [{name}] 登录态中无 accessToken，请重新登录")
                continue
            if token in seen:
                continue
            seen.add(token)
            # 仅内存替换，不写回文件
            session["auth"]["accessToken"] = token
            accounts.append((name, session))
        except Exception as e:
            log(f"ERROR: 解析 [{name}] 失败：{e}")
    return accounts


def api(path: str, session: dict) -> dict:
    auth = session["auth"]
    headers = {
        "Authorization": f"Bearer {auth['accessToken']}",
        "X-User-Id": session["account"]["uid"],
        "X-Domain": auth.get("domain", ""),
        "Content-Type": "application/json",
    }
    r = requests.post(f"{API_BASE}/{path}", headers=headers, json={}, timeout=30)
    return r.json()


def checkin_api(name: str, session: dict) -> bool:
    """HTTP API 签到，成功返回 True"""
    # 检查 accessToken 是否过期（毫秒时间戳；过期时需手动打开
    # WorkBuddy 刷新登录态文件后重试）
    expires_at = (session.get("auth") or {}).get("expiresAt") or 0
    if expires_at / 1000 < time.time():
        log(f"[{name}] accessToken 已过期，需启动 WorkBuddy 刷新登录态")
        return False

    try:
        status = api("checkin-status", session)
    except Exception as e:
        log(f"[{name}] 查询签到状态失败：{e}")
        return False

    data = status.get("data") or {}
    if status.get("code") != 0:
        log(f"[{name}] 查询签到状态异常：{status.get('msg')}")
        return False
    if data.get("today_checked_in"):
        log(f"[{name}] (API) 今日已签到")
        return True

    try:
        claim = api("daily-checkin", session)
    except Exception as e:
        log(f"[{name}] 领取积分失败：{e}")
        return False

    if claim.get("code") == 0:
        credit = (claim.get("data") or {}).get("credit", "?")
        log(f"[{name}] (API) 签到成功，获得积分：{credit}")
        return True
    if claim.get("code") == 10001:  # 今天已签到，请明天再来
        log(f"[{name}] (API) 今日已签到")
        return True
    log(f"[{name}] (API) 签到失败：{claim.get('msg')}")
    return False


def main() -> int:
    log("=" * 50)
    log("[WorkBuddy] 开始每日积分领取")
    log("=" * 50)
    accounts = load_accounts()
    if not accounts:
        log("ERROR: 没有可用登录态，请先打开 WorkBuddy 并登录")
        return 1
    results = [checkin_api(name, session) for name, session in accounts]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
