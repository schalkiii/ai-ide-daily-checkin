# -*- coding: utf-8 -*-
"""豆包 PC 客户端每日签到（脱机 API 直调）。

思路（与本项目其余家一致：只读本地登录态 -> 直调官方同源接口）：
    读 Chromium Cookie 仓里的豆包登录态 -> 调 campaign 接口领取每日签到额度。
    - 完全脱机：不打开客户端、不模拟 UI
    - 只读 cookie 文件，绝不修改；不打印 cookie/token 明文
    - 幂等：detail 显示今日已签则直接跳过

凭证链（os_crypt 方案，与 checkin_qoder.py / checkin_kuku.py 相同）：
    %LOCALAPPDATA%\\Doubao\\User Data\\Local State
        -> os_crypt.encrypted_key(base64, 前缀 "DPAPI") -> CryptUnprotectData -> 32B AES-256 key
    %LOCALAPPDATA%\\Doubao\\User Data\\Default\\Network\\Cookies (SQLite)
        -> encrypted_value = "v10"/"v11" + 12B nonce + 密文 + 16B tag（AES-256-GCM）
        -> Chromium 130+ 明文前 32B 是 SHA256(host_key)（域绑定），需剥掉
    豆包运行时会独占 Cookie 库（Windows 共享冲突），此时回退同目录缓存
    doubao_auth.json（首次需在豆包关闭时跑一次建立缓存，之后每次仍会用接口校验）。

接口（逆向自 PC 客户端 ugCampaign 模块）：
    POST /alice/ug/campaign/landing/get      {"scene": <group>}
    POST /alice/ug/campaign/v3/detail/       {"campaign_group_code": <group>}
        -> tasks[daily_signin].daily_checkin.checked_in_today / continuous_days / reward
    POST /alice/ug/campaign/landing/checkin  {"scene": <group>}
    公共 query 参数（aid=582478、device_id、web_id、fp 等）从客户端日志
    sdk_storage/log/saman_*.log 里最近的 PC 请求 URL 解析；缺失时依次用缓存、最小集。
    活动 group = pc_incentive_task_mvp_v3，每日 +200 额度。

已知服务端问题（2026-10-22 实测）：
    官方 PC 客户端弹窗点「立即签到」同样返回 710010702「系统错误」，报文与抓包完全一致，
    因此本脚本对 710010702 如实报失败（不伪报成功）；服务端恢复后无需改代码即可签到。

用法：
    python checkin_doubao.py            # 查询状态并签到
    python checkin_doubao.py --dry-run  # 只查询，不领取
    python checkin_doubao.py --status   # 打印 campaign detail 原始 JSON
"""
import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DOUBAO_DATA = Path(os.environ.get("LOCALAPPDATA", "")) / "Doubao" / "User Data"
LOCAL_STATE = DOUBAO_DATA / "Local State"
COOKIE_DB = DOUBAO_DATA / "Default" / "Network" / "Cookies"
LOG_DIR = DOUBAO_DATA / "sdk_storage" / "log"
COOKIE_HOST = "www.doubao.com"

API_BASE = "https://www.doubao.com"
SCENE = "pc_incentive_task_mvp_v3"
LANDING_PATH = "/alice/ug/campaign/landing/get"
DETAIL_PATH = "/alice/ug/campaign/v3/detail/"
CHECKIN_PATH = "/alice/ug/campaign/landing/checkin"

# 从客户端请求 URL 里保留的公共 query（PC 客户端口径，aid=582478）
COMMON_KEYS = (
    "version_code", "language", "device_platform", "doubao_device_platform",
    "aid", "real_aid", "pkg_type", "device_id", "pc_version", "doubao_pc_version",
    "web_id", "tea_uuid", "region", "sys_region", "samantha_web", "web_platform",
    "use-olympus-account", "runtime", "runtime_version", "client_platform",
    "chromium_version", "channel", "fp",
)
# 日志里解析不到时的最小可用集（实测 landing/get、v3/detail 均能返回 code=0）
MINIMAL_COMMON = {
    "version_code": "20800",
    "language": "zh",
    "device_platform": "web",
    "doubao_device_platform": "desktop",
    "aid": "582478",
    "real_aid": "582478",
    "pkg_type": "release_version",
    "client_platform": "pc_client",
}
URL_RE = re.compile(r"https://www[.]doubao[.]com/\S+")
LOG_TAIL_BYTES = 16 * 1024 * 1024

BASE_DIR = Path(__file__).parent
CACHE_FILE = BASE_DIR / "doubao_auth.json"
LOG_FILE = BASE_DIR / "checkin.log"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---- DPAPI / Chromium cookie 解密 ----
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_unprotect(data: bytes) -> bytes:
    kernel32 = ctypes.windll.kernel32
    inp, out = _Blob(), _Blob()
    inp.cbData = len(data)
    inp.pbData = ctypes.create_string_buffer(data)
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError(f"CryptUnprotectData failed, gle={kernel32.GetLastError()}（需当前用户处于交互登录态）")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _os_crypt_key() -> bytes:
    with open(LOCAL_STATE, encoding="utf-8") as fh:
        raw = base64.b64decode(json.load(fh)["os_crypt"]["encrypted_key"])
    if raw[:5] != b"DPAPI":
        raise ValueError("unexpected encrypted_key prefix")
    return _dpapi_unprotect(raw[5:])


def _cookie_domain_matches(host_key: str) -> bool:
    domain = host_key.lstrip(".").lower()
    return COOKIE_HOST == domain or COOKIE_HOST.endswith("." + domain)


def _secure_value(text: str) -> bool:
    """Cookie 值要能放进 HTTP 头（latin-1、无控制字符）。"""
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def _decrypt_cookie(blob: bytes, host_key: str, key: bytes) -> str | None:
    if blob[:3] not in (b"v10", b"v11") or len(blob) <= 31:
        return None
    plain = AESGCM(key).decrypt(blob[3:15], blob[15:], None)
    binding = hashlib.sha256(host_key.encode()).digest()
    if plain.startswith(binding):  # Chromium 130+ 域绑定
        plain = plain[32:]
    return plain.decode("utf-8", "replace")


def load_cookie_from_db() -> str | None:
    """读 Cookie 仓并拼 Cookie 头；豆包运行时文件被独占，抛 PermissionError。"""
    if not COOKIE_DB.exists():
        return None
    key = _os_crypt_key()
    tmpdir = Path(tempfile.mkdtemp(prefix="doubao_cookies_"))
    tmp_db = tmpdir / "Cookies"
    try:
        shutil.copy2(COOKIE_DB, tmp_db)  # 客户端关闭时可复制
        for suffix in ("-wal", "-shm"):
            side = COOKIE_DB.with_name(COOKIE_DB.name + suffix)
            if side.exists():
                shutil.copy2(side, tmpdir / ("Cookies" + suffix))
        con = sqlite3.connect(str(tmp_db))
        try:
            rows = list(con.execute(
                "SELECT host_key, name, encrypted_value, value, expires_utc FROM cookies"))
        finally:
            con.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    now = datetime.now().timestamp()
    cookies: dict[str, str] = {}
    # 更具体的 host（www.doubao.com）优先于 .doubao.com
    for host_key, name, encrypted, value, expires_utc in sorted(rows, key=lambda r: -len(r[0])):
        if name in cookies or not _cookie_domain_matches(host_key):
            continue
        if expires_utc:  # Chromium: 1601-01-01 起的微秒
            expires = expires_utc / 1_000_000 - 11644473600
            if 0 < expires < now:
                continue
        blob = bytes(encrypted) if encrypted else b""
        text = None
        if blob:
            try:
                text = _decrypt_cookie(blob, host_key, key)
            except Exception:
                continue
        elif value:
            text = value
        if text and _secure_value(text):
            cookies[name] = text
    if not cookies:
        return None
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


# ---- 缓存（豆包运行时 Cookie 库被独占）----
def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(cookie_header: str, params: dict) -> None:
    try:
        CACHE_FILE.write_text(json.dumps({
            "cookie_header": cookie_header,
            "common_params": params,
            "verified_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# ---- 公共 query 参数 ----
def common_params_from_log() -> dict:
    """从最新的客户端日志尾部抓一条 PC 请求 URL，复用其公共 query。"""
    if not LOG_DIR.is_dir():
        return {}
    logs = sorted(LOG_DIR.glob("saman_2*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in logs[:3]:
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - LOG_TAIL_BYTES))
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        found = {}
        for match in URL_RE.finditer(text):
            query = dict(parse_qsl(urlsplit(match.group(0)).query))
            if query.get("aid") == "582478" and query.get("client_platform") == "pc_client":
                found = query  # 保留最后一条（最新）
        if found:
            return {k: found[k] for k in COMMON_KEYS if k in found}
    return {}


def resolve_common_params(cache: dict) -> dict:
    params = common_params_from_log()
    source = "客户端日志"
    if not params:
        params = cache.get("common_params") or {}
        source = "缓存"
    if not params:
        params = dict(MINIMAL_COMMON)
        source = "最小集"
    for key, value in MINIMAL_COMMON.items():
        params.setdefault(key, value)
    params.pop("msToken", None)
    params.pop("a_bogus", None)
    params["web_tab_id"] = str(uuid.uuid4())  # 每次会话一个，无关签名
    log(f"公共参数来源：{source}（device_id={params.get('device_id', '缺失')}）")
    return params


def headers(cookie_header: str, params: dict) -> dict:
    version = params.get("pc_version", "")
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/147.0.0.0 Safari/537.36 SamanthaDoubao/" + (version or "2.32.6"))
    return {
        "Content-Type": "application/json",
        "Origin": API_BASE,
        "Referer": f"{API_BASE}/pc/agent-explore",
        "User-Agent": ua,
        "Agw-Js-Conv": "str",
        "Accept": "application/json, text/plain, */*",
        "Cookie": cookie_header,
    }


def api(path: str, cookie_header: str, params: dict, body: dict) -> dict:
    r = requests.post(f"{API_BASE}{path}", params=params,
                      headers=headers(cookie_header, params), json=body, timeout=30)
    try:
        return r.json()
    except ValueError:
        return {"code": -1, "msg": f"HTTP {r.status_code} 非 JSON 响应：{r.text[:200]}"}


# ---- 签到流程 ----
def daily_signin_of(detail: dict) -> dict | None:
    for task in (detail.get("data") or {}).get("tasks") or []:
        if task.get("task_kind") == "daily_signin":
            return task.get("daily_checkin") or {}
    return None


def candidate_cookies(cache: dict) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    try:
        cookie = load_cookie_from_db()
        if cookie:
            candidates.append(("本地 Cookie 仓", cookie))
    except PermissionError:
        log("Cookie 库被豆包独占（客户端运行中），改用缓存")
    except FileNotFoundError:
        log(f"未找到登录态：{LOCAL_STATE}")
    except Exception as e:
        log(f"读取本地登录态异常：{e.__class__.__name__}: {e}")
    cached = cache.get("cookie_header")
    if cached and cached not in (c[1] for c in candidates):
        candidates.append(("缓存 doubao_auth.json", cached))
    return candidates


def resolve_session(cache: dict, params: dict) -> tuple[str | None, dict | None]:
    """逐个候选调 landing/get 校验登录态，成功才写缓存。"""
    last = None
    for source, cookie in candidate_cookies(cache):
        doc = api(LANDING_PATH, cookie, params, {"scene": SCENE})
        last = doc
        if doc.get("code") == 0:
            log(f"登录态有效（来源={source}）")
            save_cache(cookie, params)
            return cookie, doc
        log(f"  来源[{source}] 登录态无效：code={doc.get('code')} {doc.get('msg')}")
    return None, last


def main() -> int:
    ap = argparse.ArgumentParser(description="豆包 PC 客户端每日签到")
    ap.add_argument("--dry-run", action="store_true", help="只查询状态，不领取")
    ap.add_argument("--status", action="store_true", help="打印 campaign detail 原始 JSON")
    args = ap.parse_args()

    cache = load_cache()
    params = resolve_common_params(cache)
    log("=" * 46)
    log("[豆包] 开始每日签到")

    cookie, landing = resolve_session(cache, params)
    if cookie is None:
        if landing is None:
            log("ERROR: 未读取到任何豆包登录态。请先登录豆包；"
                "若豆包正在运行（Cookie 库被独占），请关闭后运行一次以建立缓存")
        else:
            log(f"ERROR: 豆包登录态无效 code={landing.get('code')} {landing.get('msg')}。"
                "请打开豆包刷新登录态后重试")
        return 1

    detail = api(DETAIL_PATH, cookie, params, {"campaign_group_code": SCENE})
    if detail.get("code") != 0:
        log(f"查询签到详情失败：code={detail.get('code')} {detail.get('msg')}")
        return 1
    if args.status:
        print(json.dumps(detail, ensure_ascii=False, indent=2))
        return 0

    signin = daily_signin_of(detail)
    if signin is None:
        log("活动详情中没有 daily_signin 任务（活动可能已改版）")
        return 1
    reward = (signin.get("reward") or {}).get("reward_amount", "?")
    days = signin.get("total_days", "?")
    streak = signin.get("continuous_days", "?")

    if signin.get("checked_in_today"):
        log(f"今日已签到（连签 {streak} 天，累计 {days} 天）")
        return 0

    log(f"今日未签到：可领 +{reward} 额度（连签 {streak} 天，累计 {days} 天）")
    if args.dry_run:
        log("[dry-run] 不领取")
        return 0

    result = api(CHECKIN_PATH, cookie, params, {"scene": SCENE})
    if result.get("code") == 0 and (result.get("data") or {}).get("checked_in"):
        after = api(DETAIL_PATH, cookie, params, {"campaign_group_code": SCENE})
        info = daily_signin_of(after) or signin
        log(f"签到成功，+{(info.get('reward') or {}).get('reward_amount', reward)} 额度"
            f"（连签 {info.get('continuous_days', streak)} 天，累计 {info.get('total_days', days)} 天）")
        return 0

    # 报文可能超时/失败但服务端已入账，复查一次避免误报
    after = api(DETAIL_PATH, cookie, params, {"campaign_group_code": SCENE})
    info = daily_signin_of(after) or {}
    if info.get("checked_in_today"):
        log(f"今日已签到（连签 {info.get('continuous_days', '?')} 天）")
        return 0

    code = result.get("code")
    if code == 710010702:
        log("签到失败：服务端系统错误 710010702（系统错误）；"
            "官方 PC 客户端弹窗同样失败，非本脚本问题，请稍后重试")
    else:
        log(f"签到失败：code={code} {result.get('msg')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
