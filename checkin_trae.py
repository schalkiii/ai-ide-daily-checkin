# -*- coding: utf-8 -*-
"""Trae CN / TRAE SOLO CN 每日自动签到。

思路（来自 linux.do 帖子）：只读本地登录态与设备 ID 文件 -> 解出 token -> 调官方签到接口。
    - 只读登录态与设备 ID 文件，不修改任何文件
    - 不打印 token
    - 结果写入同目录 checkin.log

凭证：
    %APPDATA%\\Trae CN\\User\\globalStorage\\storage.json
        {"iCubeAuthInfo://icube.cloudide": "<base64 密文>"}
    自定义加密（非标准 DPAPI）：
        t = base64.b64decode(enc)
        key = t[6:38]                                   # 32B key
        sha = sha512(key)
        xor = URE ^ DRE                                 # URE/DRE 为硬编码常量
        h = sha512(sha + xor)
        aes_key, iv = h[:16], h[16:32]
        ct = t[38:]
        plain = AES-CBC(unpad(decrypt(ct)))             # 去掉前 64B HMAC
        json.loads(plain[64:])

签到接口：
    POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/status
    POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/claim
    Headers：Authorization: Cloud-IDE-JWT <token> + x-device-id + X-User-Region

最近过期额度（推送用）：
    POST https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage  {"require_usage": true}
        user_entitlement_pack_list[]：expire_time 到期、quota.credits_limit 总量、
        usage.credits_amount 已用；跳过已用尽的包，取剩余包中最早到期者。
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# ---- 配置 ----
AUTH_KEY = "iCubeAuthInfo://icube.cloudide"
API_BASE = "https://api.trae.cn/trae/api/v2/ug/checkin_credits"
ENTITLEMENT_API = "https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage"
REQ_SOURCE = 1  # 1=Trae CN IDE；2=SOLO/企业版（同一账号积分互通，用 1 即可）

# iCube 登录态解密常量（与 Trae 客户端加密格式对应）
HDR_LEN = 6
KEY_LEN = 32
HMAC_LEN = 64
URE = bytes([82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251, 124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203, 84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78, 8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37])
DRE = bytes([31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95, 96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239, 160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97, 23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125])

APP_DIRS = ["Trae CN"]
EXTRA_PATHS = [
    # r"%APPDATA%\TRAE SOLO CN",   # 需要同时覆盖 SOLO 版时取消注释
]

DEVICE_ID_FALLBACK = ""  # 兜底设备 ID，一般可自动读取

BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / "checkin.log"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def decrypt_auth(b64_text: str) -> dict:
    """解密 iCubeAuthInfo 加密串，返回含 token / userRegion 的 dict"""
    t = base64.b64decode(b64_text)
    key = t[HDR_LEN:HDR_LEN + KEY_LEN]
    sha = hashlib.sha512(key).digest()
    xor = bytes(a ^ b for a, b in zip(URE, DRE))
    h = hashlib.sha512(sha + xor).digest()
    aes_key, iv = h[:16], h[16:32]
    ct = t[HDR_LEN + KEY_LEN:]
    plain = unpad(AES.new(aes_key, AES.MODE_CBC, iv).decrypt(ct), AES.block_size)
    return json.loads(plain[HMAC_LEN:].decode("utf-8"))


def scan_targets() -> list:
    """扫描目标 [(名称, storage.json 路径, 应用数据目录)]，按路径去重"""
    appdata = os.environ.get("APPDATA", "")
    targets = [(n, Path(appdata) / n / "User" / "globalStorage" / "storage.json",
                Path(appdata) / n) for n in APP_DIRS]
    for raw in EXTRA_PATHS:
        p = Path(os.environ.get("USERPROFILE", "") + raw.replace("%USERPROFILE%", "")).expanduser()
        if p.suffix.lower() == ".json":
            app_dir = p.parents[2] if len(p.parents) > 2 else p.parent
            targets.append((p.parent.name, p, app_dir))
        else:
            targets.append((p.name, p / "User" / "globalStorage" / "storage.json", p))
    seen, uniq = set(), []
    for name, sf, d in targets:
        if str(sf).lower() not in seen:
            seen.add(str(sf).lower())
            uniq.append((name, sf, d))
    return uniq


def load_accounts() -> list:
    """从各客户端 storage.json 读取账号，按 token 去重"""
    accounts, seen = [], set()
    for name, path, _ in scan_targets():
        try:
            if not path.exists():
                log(f"跳过 [{name}]：未找到 {path}")
                continue
            storage = json.loads(path.read_text(encoding="utf-8"))
            enc = storage.get(AUTH_KEY)
            if not enc:
                log(f"跳过 [{name}]：storage.json 中无登录态")
                continue
            auth = decrypt_auth(enc)
            token = auth.get("token")
            if not token:
                log(f"ERROR: [{name}] 登录态中无 token，请重新登录")
                continue
            if token in seen:
                continue
            seen.add(token)
            accounts.append((name, auth))
        except Exception as e:
            log(f"ERROR: 解析 [{name}] 失败：{e}")
    return accounts


def load_device_id() -> str:
    """设备 ID：ahanet 配置 -> 客户端日志 -> 兜底常量"""
    dirs = [d for _, _, d in scan_targets()]
    for d in dirs:
        p = d / "ahanet" / "tt_net_config.config"
        try:
            m = re.search(r"device_id[^\w]*(\d+)", p.read_text(encoding="utf-8", errors="ignore"))
            if m:
                return m.group(1)
        except OSError:
            continue
    for d in dirs:
        logs_dir = d / "logs"
        if not logs_dir.is_dir():
            continue
        try:
            main_logs = sorted(logs_dir.glob("*/main.log"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        for p in main_logs[:5]:
            try:
                m = re.search(r"\[ICDRS\].*?did: (\d+)",
                              p.read_text(encoding="utf-8", errors="ignore"))
                if m:
                    return m.group(1)
            except OSError:
                continue
    return DEVICE_ID_FALLBACK


def auth_headers(token: str, region: str, device_id: str) -> dict:
    headers = {
        "Authorization": f"Cloud-IDE-JWT {token}",
        "Content-Type": "application/json",
        "x-device-id": device_id,
    }
    if region:
        headers["X-User-Region"] = region
    return headers


def api(url: str, token: str, region: str, device_id: str) -> dict:
    r = requests.post(url, headers=auth_headers(token, region, device_id),
                      json={"req_source": REQ_SOURCE}, timeout=30)
    r.raise_for_status()
    return r.json()


def fmt_num(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def expiring_note(name: str, token: str, region: str, device_id: str) -> str:
    """最近到期且仍有剩余的积分包摘要；已用尽的不算，查询失败返回空串。"""
    try:
        r = requests.post(ENTITLEMENT_API, headers=auth_headers(token, region, device_id),
                          json={"require_usage": True, "req_source": REQ_SOURCE}, timeout=30)
        r.raise_for_status()
        packs = (r.json() or {}).get("user_entitlement_pack_list") or []
    except Exception as e:
        log(f"[{name}] 额度包查询失败（已忽略）：{e}")
        return ""

    now, best = time.time(), None
    for pack in packs:
        info = pack.get("entitlement_base_info") or {}
        extra = (info.get("product_extra") or {}).get("package_extra") or {}
        quota = extra.get("quota") or info.get("quota") or {}
        total = quota.get("credits_limit")
        expire = pack.get("expire_time") or info.get("end_time") or 0
        if not total or expire <= now:
            continue
        used = (pack.get("usage") or {}).get("credits_amount") or 0
        if total - used <= 0:
            continue
        if best is None or expire < best[3]:
            best = (extra.get("package_name") or pack.get("display_desc") or "积分包",
                    total, used, expire)
    if best is None:
        return ""
    name, total, used, expire = best
    expires = datetime.fromtimestamp(expire)
    return (f"；最近过期：{name}（{expires:%Y-%m-%d %H:%M}）"
            f"已用 {fmt_num(used)}/{fmt_num(total)}（剩 {fmt_num(total - used)}）")


def unwrap(resp: dict) -> dict:
    """兼容 {checked_in:...} 与 {code:0, data:{checked_in:...}} 两种返回格式"""
    if ("checked_in" not in resp and isinstance(resp.get("data"), dict)
            and "checked_in" in resp["data"]):
        return resp["data"]
    return resp


def checkin_api(name: str, auth: dict, device_id: str) -> bool:
    """HTTP API 签到，成功返回 True"""
    token = auth["token"]
    region = (auth.get("userRegion") or {}).get("region")
    try:
        status = unwrap(api(f"{API_BASE}/status", token, region, device_id))
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        log(f"[{name}] 查询签到状态失败(HTTP {code})，登录态可能过期")
        return False
    except Exception as e:
        log(f"[{name}] 网络错误：{e}")
        return False

    if status.get("checked_in"):
        log(f"[{name}] (API) 今日已签到，当前积分：{status.get('credits')}"
            f"{expiring_note(name, token, region, device_id)}")
        return True
    if not status.get("enable"):
        log(f"[{name}] 签到不可用：{json.dumps(status, ensure_ascii=False)[:200]}")
        return False

    try:
        claim = api(f"{API_BASE}/claim", token, region, device_id)
    except Exception as e:
        log(f"[{name}] 领取积分失败：{e}")
        return False

    if claim.get("code") == 0:
        try:
            after = unwrap(api(f"{API_BASE}/status", token, region, device_id))
            log(f"[{name}] (API) 签到成功，当前积分：{after.get('credits', '?')}"
                f"{expiring_note(name, token, region, device_id)}")
        except Exception:
            log(f"[{name}] (API) 签到成功")
        return True
    log(f"[{name}] (API) 签到失败：{claim.get('message') or json.dumps(claim, ensure_ascii=False)[:200]}")
    return False


def main() -> int:
    accounts = load_accounts()
    if not accounts:
        log("ERROR: 没有可用登录态，请先打开 TRAE SOLO CN / Trae CN 并登录")
        return 1
    device_id = load_device_id()
    results = [checkin_api(name, auth, device_id) for name, auth in accounts]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
