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
from datetime import datetime, timedelta
from pathlib import Path

import requests
from Crypto.Cipher import AES

import credits_common as cc

# ---- 配置 ----
API_BASE = "https://copilot.tencent.com/v2/billing/meter"
AUTH_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "CodeBuddyExtension" / "Data" / "Public" / "auth"
# 积分权益包列表（签名与 UA 硬校验均逆向自客户端 out/main.js）
CREDITS_API = "https://copilot.tencent.com/v2/billing/meter/get-user-resource"
CREDITS_USER_AGENT = "CodeBuddyCN"  # 服务端校验 UA，缺失/不符返回 403 code=10085

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


def fetch_credits(name: str, session: dict) -> dict:
    """查询 WorkBuddy 积分余额与权益包过期时间（2026-10-22 实测可用）。

    POST /v2/billing/meter/get-user-resource（调用签名逆向自客户端 out/main.js
    的 getPersonalUsage）：分页列出积分账户（权益包），每包含 CapacityRemain
    余额与 CycleEndTime 过期时间（30 天周期）。Status=0 为活跃。
    注意：必须带 User-Agent: CodeBuddyCN，否则 403。
    """
    auth = session["auth"]
    expires_at = auth.get("expiresAt") or 0
    if expires_at / 1000 < time.time():
        return {"error": "accessToken 已过期，需启动 WorkBuddy 刷新登录态"}

    now = datetime.now()
    headers = {
        "Authorization": f"Bearer {auth['accessToken']}",
        "X-User-Id": session["account"]["uid"],
        "User-Agent": CREDITS_USER_AGENT,
        "Content-Type": "application/json",
    }
    body = {
        "PageNumber": 1, "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": now.strftime("%Y-%m-%d %H:%M:%S"),
        "PackageEndTimeRangeEnd": (now + timedelta(days=101 * 365)).strftime("%Y-%m-%d %H:%M:%S"),
    }
    r = requests.post(CREDITS_API, headers=headers, json=body, timeout=30)
    doc = r.json()
    if doc.get("code") != 0:
        return {"error": f"HTTP {r.status_code} code={doc.get('code')} {doc.get('msg')}"}
    inner = ((doc.get("data") or {}).get("Response") or {}).get("Data") or {}
    accounts = inner.get("Accounts") or []

    packs = []
    for acct in accounts:
        if acct.get("Status") != 0:
            continue  # 仅活跃包
        remain = acct.get("CapacityRemain") or 0
        expire = cc.parse_ts(acct.get("CycleEndTime"))
        if remain <= 0 or expire is None:
            continue
        packs.append({"desc": acct.get("PackageName") or "", "credits": remain,
                      "expire_dt": expire})
    packs.sort(key=lambda p: p["expire_dt"])

    result = {
        "remaining": sum(p["credits"] for p in packs),
        "packs": [{"desc": p["desc"], "credits": p["credits"],
                   "expire": cc.fmt_dt(p["expire_dt"])} for p in packs[:10]],
        "expiring_7d": cc.sum_expiring(packs, 7),
    }
    if packs:
        result["nearest"] = {"credits": packs[0]["credits"],
                             "expire": cc.fmt_dt(packs[0]["expire_dt"])}
    return result


def main() -> int:
    log("=" * 50)
    log("[WorkBuddy] 开始每日积分领取")
    log("=" * 50)
    accounts = load_accounts()
    if not accounts:
        log("ERROR: 没有可用登录态，请先打开 WorkBuddy 并登录")
        return 1
    results = [checkin_api(name, session) for name, session in accounts]
    rc = 0 if all(results) else 1
    # 积分余额与过期（查询失败不影响签到退出码）
    for name, session in accounts:
        try:
            summary = fetch_credits(name, session)
        except Exception as e:
            summary = {"error": str(e)}
        log("[CREDITS] " + json.dumps(summary, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
