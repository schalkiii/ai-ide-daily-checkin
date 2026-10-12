# -*- coding: utf-8 -*-
"""库库AI (GenFlowPro / kuku.baidu.com) 每日积分脱机签到。

思路（与本项目 Qoder/Trae/WorkBuddy 一致：只读本地登录态 -> 直调官方接口）：
    读本地 Chromium Cookie 仓里的百度 passport BDUSS/STOKEN -> 直调 freepoint 接口领取每日积分。
    - 完全脱机：不打开客户端、不模拟 UI、不逆向签名（鉴权仅靠 BDUSS cookie，无 Authorization/nonce/ctoken）
    - 只读 cookie 文件，绝不修改；不打印 cookie 明文
    - 签到幂等：daily_login/daily_chat 领取后变 FINISHED，重复运行安全

凭证链（os_crypt 方案，与 checkin_qoder.py 相同；CryptUnprotectData 需当前 Windows 用户处于交互登录态）：
    %APPDATA%\\baidugenflowpro\\Local State
        -> os_crypt.encrypted_key(base64, 前缀 "DPAPI") -> CryptUnprotectData -> 32B AES-256 key
    %APPDATA%\\baidugenflowpro\\Network\\Cookies  (SQLite，默认上下文，freepoint 的 STOKEN 在此)
        -> host 如 .baidu.com 的 BDUSS，.kuku.baidu.com 的 STOKEN，encrypted_value = "v10"+12B nonce+密文+16B tag（AES-256-GCM）
    （Partitions\\genflow-auth 分区仓的 STOKEN 常陈旧，仅作兜底，因此默认仓候选在前。）
    客户端运行时会独占该 DB：读取失败时回退到同目录缓存 kuku_auth.json。
    cookie 来源（DB / 缓存）会先用 homenew 校验，只有 errno=0 的才回写缓存，
    因此陈旧 STOKEN（客户端被强杀未落盘时）不会污染缓存；BDUSS 约 30 天有效。

接口（逆向自客户端 app.asar 的 freepoint 模块，Go 端 RewardClaimReqDto）：
    GET  /api/genflowpro/freepoint/homenew?app_id=123971023&channel=chunlei&version=1.6.9&clienttype=401
    POST /api/genflowpro/freepoint/rewardClaim?<同上 query>
         body(x-www-form-urlencoded): activity_key, period_id(=homenew 的 period_no), task_key
    响应 errno=0；data.claim_status=SUCCESS 领取成功；task_status: UNFINISHED / FINISHED
    两个每日奖励：daily_login(每日登录 +50)、daily_chat(完成一次对话 +50，需先真实对话才变 claimable)

用法：
    python checkin_kuku.py            # 读取登录态并领取所有可领取任务
    python checkin_kuku.py --dry-run  # 只列出当前活动/可领取任务，不真正领取
    python checkin_kuku.py --status   # 打印 homenew 原始 JSON
"""
import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

APP_DIR = Path(os.environ.get("APPDATA", "")) / "baidugenflowpro"
# freepoint 实际使用的登录态在“默认上下文” Cookie 仓（native http 注入的 cookie 来自
# login-domain-plus-all-session）；genflow-auth 分区仓的 STOKEN 往往是陈旧的，只作兜底。
# 必须默认仓在前：load_cookie_from_db 用 setdefault，先读到哪个 STOKEN 就用哪个。
COOKIE_DB_CANDIDATES = [
    APP_DIR / "Network" / "Cookies",  # 默认上下文：含有效的 genflowpro STOKEN
    APP_DIR / "Partitions" / "genflow-auth" / "Network" / "Cookies",  # 认证分区（可能陈旧），补充
]
LOCAL_STATE = APP_DIR / "Local State"

API_BASE = "https://kuku.baidu.com/api/genflowpro/freepoint"
# 客户端 freepoint 模块经 native http 注入的公共 query；缺任一会返回 errno=2 params error
COMMON = {
    "app_id": "123971023",
    "channel": "chunlei",
    "version": "1.6.9",
    "clienttype": "401",  # PCWindows
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "KuKuGenFlow/1.6.9 Chrome/146.0.7680.216 Electron/41.10.1 Safari/537.36"
      ";genflow;1.6.9;PC;PC-Windows;10.0.28000;GenFlowPro")
# 需要携带的百度 passport cookie（BDUSS 必需，其余一并带上更稳）
WANTED = ("BDUSS", "STOKEN", "PTOKEN", "BAIDUID_BFESS", "BDUSS_BFESS", "gfprotpl")

BASE_DIR = Path(__file__).parent
CACHE_FILE = BASE_DIR / "kuku_auth.json"
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


def _read_cookie_db(db_path: Path, key: bytes) -> dict:
    """从（可能被客户端独占的）SQLite Cookie 仓读取并解密目标 cookie。"""
    if not db_path.exists():
        return {}
    tmp = Path(tempfile.gettempdir()) / f"kuku_cookies_{os.getpid()}.db"
    try:
        shutil.copy2(db_path, tmp)  # 客户端关闭时可复制
    except OSError:
        raise  # 客户端运行时独占 -> 上层回退缓存
    try:
        con = sqlite3.connect(str(tmp))
        out = {}
        for host, name, ev, val in con.execute(
                "SELECT host_key, name, encrypted_value, value FROM cookies"):
            if name not in WANTED or name in out:
                continue
            blob = bytes(ev) if ev else b""
            if blob[:3] in (b"v10", b"v11") and len(blob) > 15:
                out[name] = AESGCM(key).decrypt(blob[3:15], blob[15:], None).decode("utf-8", "replace")
            elif val:  # 少数 cookie 以明文存在 value 列（encrypted_value 为 NULL）
                out[name] = val
        con.close()
        return out
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(tmp) + suffix)
            except OSError:
                pass


def load_cookie_from_db() -> str | None:
    """遍历候选 Cookie 仓，返回拼好的 Cookie 头字符串；全部失败返回 None。"""
    key = _os_crypt_key()
    collected = {}
    for db in COOKIE_DB_CANDIDATES:
        try:
            for name, val in _read_cookie_db(db, key).items():
                collected.setdefault(name, val)
        except OSError as e:
            log(f"  读取 {db.name} 失败（客户端可能正在运行）：{e.__class__.__name__}")
        except Exception as e:
            log(f"  解密 {db.name} 失败：{e}")
    if not collected.get("BDUSS"):
        return None
    return "; ".join(f"{k}={v}" for k, v in collected.items())


def load_cache() -> str | None:
    try:
        d = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        ck = d.get("cookie_header") or "; ".join(
            f"{k}={v}" for k, v in (d.get("cookies") or {}).items())
        return ck or None
    except (OSError, ValueError):
        return None


def save_cache(cookie_header: str) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(
            {"cookie_header": cookie_header, "cached_at": datetime.now().isoformat(timespec="seconds")}
        ), encoding="utf-8")
    except OSError:
        pass


def candidate_cookies() -> list[tuple[str, str]]:
    """按优先级返回 (来源, Cookie头) 候选：本地 Cookie 仓在前，缓存兜底在后。"""
    cands: list[tuple[str, str]] = []
    try:
        ck = load_cookie_from_db()
        if ck:
            cands.append(("本地 Cookie 仓", ck))
    except FileNotFoundError:
        log(f"未找到 Local State / Cookie 仓：{LOCAL_STATE}")
    except Exception as e:
        log(f"读取本地登录态异常：{e}")
    ck = load_cache()
    if ck and ck not in (c[1] for c in cands):
        cands.append(("缓存 kuku_auth.json", ck))
    return cands


def resolve_verified_cookie() -> tuple[str | None, dict | None]:
    """逐个候选调 homenew 校验登录态，返回 (有效 cookie_header, homenew JSON)。

    只有 errno==0 的候选才会写回缓存，避免把陈旧 STOKEN（客户端被强杀未落盘）
    污染 kuku_auth.json。全部无效时返回 (None, 最后一次响应)，由上层给出明确提示。
    """
    last_doc = None
    for src, ck in candidate_cookies():
        try:
            doc = homenew(ck)
        except Exception as e:
            log(f"  来源[{src}] homenew 请求异常：{e}")
            continue
        last_doc = doc
        if doc.get("errno") == 0:
            log(f"登录态有效（来源={src}）")
            if src == "本地 Cookie 仓":
                save_cache(ck)  # 仅缓存经验证有效的最新登录态
            return ck, doc
        log(f"  来源[{src}] 登录态无效：errno={doc.get('errno')} {doc.get('show_msg')}")
    return None, last_doc


def headers(cookie_header: str) -> dict:
    return {
        "User-Agent": UA,
        "Cookie": cookie_header,
        "Referer": "https://kuku.baidu.com/",
        "Origin": "https://kuku.baidu.com/",
        "Accept": "application/json",
    }


def homenew(cookie_header: str) -> dict:
    r = requests.get(f"{API_BASE}/homenew", params=COMMON, headers=headers(cookie_header), timeout=30)
    return r.json()


def claim(cookie_header: str, activity_key: str, period_id, task_key: str) -> dict:
    r = requests.post(
        f"{API_BASE}/rewardClaim", params=COMMON, headers=headers(cookie_header),
        data={"activity_key": activity_key, "period_id": str(period_id), "task_key": task_key},
        timeout=30)
    return r.json()


def tasks_of(doc: dict):
    act = (doc.get("data") or {}).get("activities") or []
    if not act:
        return None, []
    a = act[0]
    return a, [t for tab in (a.get("tabs") or []) for t in (tab.get("tasks") or [])]


def main() -> int:
    ap = argparse.ArgumentParser(description="库库AI 每日积分脱机签到")
    ap.add_argument("--dry-run", action="store_true", help="只列出可领取任务，不真正领取")
    ap.add_argument("--status", action="store_true", help="打印 homenew 原始 JSON")
    args = ap.parse_args()

    ck, doc = resolve_verified_cookie()
    if ck is None:
        if doc is None:
            log("ERROR: 未获取到任何 BDUSS。请先在库库AI 登录后运行一次（读 Cookie 仓或缓存）")
        else:
            log(f"ERROR: 本地登录态均无效 errno={doc.get('errno')}。"
                f"STOKEN 可能陈旧（客户端被强制关闭未落盘）——请正常打开库库AI 待登录刷新后退出，"
                f"或改用 CDP 项目（kuku-ai-daily-checkin）领取")
        return 1
    act, tasks = tasks_of(doc)
    if not act:
        log("当前没有可领取的活动（freepoint 活动可能已下线）")
        return 0

    if args.status:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0

    log("=" * 46)
    log(f"[库库] 活动={act.get('activity_name')} period_no={act.get('period_no')}")
    for t in tasks:
        log(f"  {t.get('task_key'):20} {t.get('task_name'):8} "
            f"status={t.get('task_status'):10} claimable={t.get('claimable_point')}")

    targets = [t for t in tasks if (t.get("claimable_point") or 0) > 0]
    if not targets:
        log("没有可领取的任务（可能今日已领，或 daily_chat 需先真实对话）")
        return 0
    if args.dry_run:
        log(f"[dry-run] 将领取：{[t['task_key'] for t in targets]}")
        return 0

    rc = 0
    for t in targets:
        res = claim(ck, act["activity_key"], act.get("period_no"), t["task_key"])
        d = res.get("data") or {}
        if res.get("errno") == 0 and d.get("claim_status") == "SUCCESS":
            log(f"  领取 {t['task_key']} 成功，+{d.get('claimed_point')} 积分")
        else:
            log(f"  领取 {t['task_key']} 失败：errno={res.get('errno')} {res.get('show_msg')}")
            rc = 1

    after, atasks = tasks_of(homenew(ck))
    total = sum((t.get("claimable_point") or 0) for t in atasks)
    log(f"领取后剩余可领积分：{total}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
