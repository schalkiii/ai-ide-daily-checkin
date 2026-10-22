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
"""
import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

import credits_common as cc

# ---- 配置 ----
AUTH_KEY = "iCubeAuthInfo://icube.cloudide"
API_BASE = "https://api.trae.cn/trae/api/v2/ug/checkin_credits"
# 积分权益账本（usage_summary + 每笔权益包的额度与过期时间）
CREDITS_API = "https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage"
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


def api(url: str, token: str, region: str, device_id: str) -> dict:
    headers = {
        "Authorization": f"Cloud-IDE-JWT {token}",
        "Content-Type": "application/json",
        "x-device-id": device_id,
    }
    if region:
        headers["X-User-Region"] = region
    r = requests.post(url, headers=headers, json={"req_source": REQ_SOURCE}, timeout=30)
    r.raise_for_status()
    return r.json()


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
        log(f"[{name}] (API) 今日已签到，当前积分：{status.get('credits')}")
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
            log(f"[{name}] (API) 签到成功，当前积分：{after.get('credits', '?')}")
        except Exception:
            log(f"[{name}] (API) 签到成功")
        return True
    log(f"[{name}] (API) 签到失败：{claim.get('message') or json.dumps(claim, ensure_ascii=False)[:200]}")
    return False


def fetch_credits(name: str, auth: dict, device_id: str) -> dict:
    """查询 Trae CN 积分余额与权益包过期时间（2026-10-22 实测可用）。

    POST /trae/api/v2/pay/ide_user_ent_usage 返回：
      usage_summary：总额度/已用（remaining = total - consumed）
      user_entitlement_pack_list：每笔权益包的 credits_limit 与 start/end 时间。
    每笔签到奖励独立成包，expire_time = 领取时间 + 30 天；免费配额等非积分包跳过。
    """
    token = auth["token"]
    region = (auth.get("userRegion") or {}).get("region")
    doc = api(CREDITS_API, token, region, device_id)

    summary = doc.get("usage_summary") or {}
    total, consumed = summary.get("total_amount"), summary.get("consumed_amount")
    remaining = (total - consumed) if (total is not None and consumed is not None) else None

    packs = []
    for pack in doc.get("user_entitlement_pack_list") or []:
        base = pack.get("entitlement_base_info") or {}
        extra = base.get("product_extra") or {}
        limit = None
        for key in ("package_extra", "subscription_extra"):
            quota = (extra.get(key) or {}).get("quota") or {}
            if "credits_limit" in quota:
                limit = quota["credits_limit"]
        expire = cc.parse_ts(pack.get("expire_time") or base.get("end_time"))
        if not limit or expire is None:
            continue  # 免费配额等无积分额度的包
        packs.append({"desc": pack.get("display_desc") or "", "credits": limit,
                      "expire_dt": expire})
    packs.sort(key=lambda p: p["expire_dt"])

    result = {
        "remaining": remaining,
        "packs": [{"desc": p["desc"], "credits": p["credits"],
                   "expire": cc.fmt_dt(p["expire_dt"])} for p in packs[:10]],
        "expiring_7d": cc.sum_expiring(packs, 7),
    }
    if packs:
        result["nearest"] = {"credits": packs[0]["credits"],
                             "expire": cc.fmt_dt(packs[0]["expire_dt"])}
    return result


def main() -> int:
    accounts = load_accounts()
    if not accounts:
        log("ERROR: 没有可用登录态，请先打开 TRAE SOLO CN / Trae CN 并登录")
        return 1
    device_id = load_device_id()
    results = [checkin_api(name, auth, device_id) for name, auth in accounts]
    rc = 0 if all(results) else 1
    # 积分余额与过期（查询失败不影响签到退出码）
    for name, auth in accounts:
        try:
            summary = fetch_credits(name, auth, device_id)
        except Exception as e:
            summary = {"error": str(e)}
        log("[CREDITS] " + json.dumps(summary, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
