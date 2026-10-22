# -*- coding: utf-8 -*-
"""Qoder CN 用量读取 / 每日签到脚本。

思路（来自 linux.do 帖子《Qoder CN、Trae CN、WorkBuddy 免费签到脚本》）：
    只读客户端本地登录态 -> 解出 token -> 直调官方 sash 接口 -> 领取 CLAIMABLE 活动

凭证链：
    %APPDATA%\\com.qodercn.app.stable\\Local State
        -> os_crypt.encrypted_key(base64, 前缀 "DPAPI")
        -> CryptUnprotectData（需当前 Windows 用户处于交互登录态）
        -> 32 字节 AES-256 密钥
    %APPDATA%\\com.qodercn.app.stable\\auth.v1.dat
        -> "v10" + 12B nonce + 密文 + 16B tag（AES-256-GCM）
        -> JSON: {token, refreshToken, expiresAt, user}

接口来自客户端 app.asar 里的 AccountService / CampaignService（同源调用）：
    /sash/api/v1/ai-conversations/credits-summary   totalCredits, peakCredits
    /sash/api/v1/ai-conversations/seat-activity     currentConsecutiveDays, ...
    /sash/api/v1/ai-conversations/credits-heatmap   {total, items[]}
    /sash/api/v1/me/campaigns                       用量面板的活动/签到坑位
    /api/v2/user/plan                               套餐/版本
    /api/v2/quota/usage                             剩余额度 userQuota / addOnQuota

安全原则：
    - 只读登录态文件，绝不修改（避免干扰客户端状态同步 / refresh 轮换）
    - 不打印 token（本脚本不输出任何 token 明文）
    - 签到幂等：服务端对重复领取返回 replayed:true，乱点无害

用法：
    python checkin_qoder.py                 # 用量汇总 + 自动签到（默认）
    python checkin_qoder.py claim           # 只签到
    python checkin_qoder.py claim --dry-run # 只列出将要领取的活动，不真正领取
    python checkin_qoder.py campaigns       # 打印原始活动 JSON
"""
import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
from datetime import datetime, timedelta

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import credits_common as cc

PROFILE = os.path.join(os.environ["APPDATA"], "com.qodercn.app.stable")
API = "https://openapi.qoder.com.cn"
CLIENT_TYPE = "10"  # 客户端日志里的 clientType，缺它 campaigns 返回空数组
VERSION = "0.3.4"
# 多账号凭证放同目录 auths.json（对象或对象列表），避免把 token 硬编码进源码
AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auths.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkin.log")
# 积分发放单账本：claim 响应里的 expiresAt 精确到笔，但 campaigns 无历史翻页，
# 只能逐日记录累积（gitignore）
LEDGER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credits_ledger.json")

# Windows 控制台默认 GBK，中文输出会花屏
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def emit(msg: str = "") -> None:
    """同时输出到控制台与 checkin.log（与其它两个脚本行为保持一致）。"""
    print(msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
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
            ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)
    ):
        raise OSError(f"CryptUnprotectData failed, GetLastError={kernel32.GetLastError()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def load_auth() -> dict:
    """解密客户端登录仓，返回 {token, refreshToken, expiresAt, user, ...}。"""
    with open(os.path.join(PROFILE, "Local State"), encoding="utf-8") as fh:
        raw = base64.b64decode(json.load(fh)["os_crypt"]["encrypted_key"])
    if raw[:5] != b"DPAPI":
        raise ValueError("unexpected encrypted_key prefix")
    key = _dpapi_unprotect(raw[5:])

    with open(os.path.join(PROFILE, "auth.v1.dat"), "rb") as fh:
        blob = fh.read()
    if blob[:3] != b"v10":
        raise ValueError("auth.v1.dat is not a v10 envelope")
    return json.loads(AESGCM(key).decrypt(blob[3:15], blob[15:], None))


def make_session(token: str) -> requests.Session:
    """公共头挂到 session 上，所有请求复用；Authorization 必须是 "Bearer <token>"
    （裸 token 报 TOKEN_INVALID）。"""
    s = requests.Session()
    s.headers.update({
        "Accept": "application/json",
        "User-Agent": "Qoder",
        "Authorization": f"Bearer {token}",
        "Cosy-ClientType": CLIENT_TYPE,
        "Cosy-Version": VERSION,
    })
    return s


def get(session: requests.Session, path: str, params: dict | None = None):
    r = session.get(API + path, params=params, timeout=20)
    if r.status_code != 200:
        return {"_error": r.status_code, "_body": r.text[:400]}
    return r.json()


ENDPOINTS = {
    "credits-summary": ("/sash/api/v1/ai-conversations/credits-summary", None),
    "seat-activity": ("/sash/api/v1/ai-conversations/seat-activity", None),
    "credits-heatmap": ("/sash/api/v1/ai-conversations/credits-heatmap", {"days": 30, "organization_id": ""}),
    "campaigns": ("/sash/api/v1/me/campaigns", None),
    "plan": ("/api/v2/user/plan", None),
    "quota": ("/api/v2/quota/usage", None),
}


def claim_one(session: requests.Session, campaign_id: str) -> dict:
    """POST /sash/api/v1/me/campaigns/{campaignId}/claim

    服务端幂等：重复领取返回 status=200 + replayed:true 和原发放单，不会重复发权益。
    """
    r = session.post(f"{API}/sash/api/v1/me/campaigns/{campaign_id}/claim", timeout=20)
    try:
        body = r.json()
    except ValueError:
        body = {"_raw": r.text[:300]}
    return {"_http": r.status_code, **body} if isinstance(body, dict) else {"_http": r.status_code, "_raw": body}


def print_quota(session: requests.Session, prefix: str = "剩余额度") -> None:
    """拉取 quota/usage 并打印可用额度；credits-summary 只有累计消耗，签到发放的记在 addOnQuota。"""
    quota = get(session, ENDPOINTS["quota"][0])
    if "_error" in quota:
        emit(f"{prefix}  : 请求失败 {quota['_error']}")
        return
    parts = []
    for label, key in (("套餐", "userQuota"), ("签到/加油包", "addOnQuota")):
        q = quota.get(key) or {}
        if q.get("total"):
            parts.append(f"{label} 剩余 {q.get('remaining'):g}/{q.get('total'):g}")
    emit(f"{prefix}  : {'，'.join(parts) if parts else '无可用额度'}"
         f"{'（已超额）' if quota.get('isQuotaExceeded') else ''}")


def do_claim(session: requests.Session, dry_run: bool) -> int:
    doc = get(session, "/sash/api/v1/me/campaigns")
    if "_error" in doc:
        emit(f"活动列表请求失败: {doc['_error']} {doc['_body'][:120]}")
        return 1
    campaigns = doc.get("campaigns") or []
    if not campaigns:
        emit("当前没有可展示的活动")
        return 0
    for c in campaigns:
        b = c.get("benefit") or {}
        emit(f"{c.get('campaignId')}  {c.get('campaignKey')}  {str(c.get('actionType', '')):13} "
             f"{str(c.get('claimStatus', '')):9} {b.get('kind')}x{b.get('amount')}")

    targets = [c for c in campaigns
               if c.get("claimStatus") == "CLAIMABLE" and c.get("actionType") == "CLAIM_BENEFIT"]
    if not targets:
        emit("\n没有 CLAIMABLE 的活动，无需签到")
        return 0
    if dry_run:
        emit(f"\n[dry-run] 将领取 {len(targets)} 条：{[t.get('campaignKey') for t in targets]}")
        return 0
    emit()
    claimed = False
    for c in targets:
        res = claim_one(session, c["campaignId"])
        b = res.get("benefit") or {}
        emit(f"签到 {c.get('campaignKey')} -> HTTP {res.get('_http')} status={res.get('status')} "
             f"replayed={res.get('replayed')} 权益={b.get('kind')}x{b.get('amount')} "
             f"发放单={res.get('grantId')}")
        if res.get("_http") == 200 and res.get("status") == "CLAIMED":
            claimed = True
    if claimed:
        emit("\n签到已到账，最新额度：")
        print_quota(session, prefix="剩余额度(签到后)")
    return 0


def show_usage(session: requests.Session, auth: dict, args) -> int:
    """usage 模式（默认）= 面板数据汇总 + 自动签到。"""
    user = auth.get("user") or {}
    summary = get(session, ENDPOINTS["credits-summary"][0])
    streak = get(session, ENDPOINTS["seat-activity"][0])
    heat = get(session, ENDPOINTS["credits-heatmap"][0],
               {"days": args.days or 30, "organization_id": ""})

    emit(f"账号      : {user.get('name')} ({user.get('id')})")
    emit(f"token 到期: {auth.get('expiresAt')}")
    print_quota(session)
    if "_error" not in summary:
        emit(f"Credits   : 累计 {summary.get('totalCredits')} / 峰值 {summary.get('peakCredits')}")
    else:
        emit(f"Credits   : 请求失败 {summary['_error']} {summary['_body'][:120]}")
    if "_error" not in streak:
        emit(f"活跃天数  : 累计 {streak.get('cumulativeActiveDays')} 天 / 当前连续 "
             f"{streak.get('currentConsecutiveDays')} 天 / 最长 {streak.get('maxConsecutiveDays')} 天"
             f"（最后活跃 {streak.get('lastActiveDate')}）")
    else:
        emit(f"活跃天数  : 请求失败 {streak['_error']}")
    if "_error" not in heat:
        items = heat.get("items") or []
        used = [i for i in items if i.get("value")]
        emit(f"热力图    : {items[0].get('date', '?') if items else '?'} ~ "
             f"{items[-1].get('date', '?') if items else '?'}，"
             f"总和 {heat.get('total')}，有用量的天数 {len(used)}")
        for i in used[-5:]:
            emit(f"            {i.get('date')}  {i.get('value')}")
    emit()
    return do_claim(session, args.dry_run)


def run_account(auth: dict, args) -> int:
    session = make_session(auth["token"])

    if args.what == "claim":
        return do_claim(session, args.dry_run)

    if args.what in ENDPOINTS:
        path, params = ENDPOINTS[args.what]
        if args.what == "credits-heatmap" and args.days:
            params = {**(params or {}), "days": args.days}
        out = get(session, path, params)
        emit(json.dumps(out, ensure_ascii=False, indent=2))
        return 1 if "_error" in out else 0

    return show_usage(session, auth, args)


def load_auths(args) -> list[dict]:
    """凭证优先级：--auth-file > 同目录 auths.json > 客户端本地解密（load_auth）。"""
    path = args.auth_file or (AUTH_FILE if os.path.exists(AUTH_FILE) else None)
    if path:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else [data]
    return [load_auth()]


# ---- 积分发放单账本（expiresAt 逐笔记录） ----

def _load_ledger() -> list:
    try:
        with open(LEDGER_FILE, encoding="utf-8") as fh:
            return (json.load(fh).get("grants")) or []
    except (OSError, ValueError):
        return []


def _save_ledger(grants: list) -> None:
    try:
        with open(LEDGER_FILE, "w", encoding="utf-8") as fh:
            json.dump({"grants": grants}, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _record_grants(session: requests.Session) -> list:
    """重放已领取 campaign 的领取请求（服务端幂等，replayed:true），
    把发放单（含 expiresAt/claimedAt）按 grantId 去重记入账本。"""
    doc = get(session, "/sash/api/v1/me/campaigns")
    if "_error" in doc:
        return _load_ledger()
    grants = _load_ledger()
    known = {g.get("grantId") for g in grants}
    added = False
    for c in doc.get("campaigns") or []:
        if c.get("claimStatus") != "CLAIMED":
            continue  # 未领取的交给 do_claim；只重放已领取的
        res = claim_one(session, c["campaignId"])
        grant_id = res.get("grantId")
        if not grant_id or grant_id in known:
            continue
        grants.append({
            "grantId": grant_id,
            "campaignKey": c.get("campaignKey"),
            "amount": (res.get("benefit") or {}).get("amount"),
            "claimedAt": res.get("claimedAt"),
            "expiresAt": res.get("expiresAt"),
        })
        known.add(grant_id)
        added = True
    if added:
        _save_ledger(grants)
    return grants


def fetch_credits(session: requests.Session) -> dict:
    """查询 Qoder CN 积分余额与过期时间（2026-10-22 实测）。

    余额：/api/v2/quota/usage 的 addOnQuota.remaining（精确）。
    过期：campaigns 端点无历史翻页（page/claimStatus/includeHistory/status
    参数均只返回当前 campaign），逐笔 expiresAt 只在 claim 响应里——对已领取的
    campaign 重放 claim 可拿回原始发放单。账本只覆盖启用本功能之后的每日记录；
    更早的历史按「每日领取、领取后 30 天有效」规则估算（标注为估算）。
    """
    doc = get(session, ENDPOINTS["quota"][0])
    if "_error" in doc:
        return {"error": f"quota 请求失败 {doc['_error']}"}
    addon = doc.get("addOnQuota") or {}
    remaining, total = addon.get("remaining"), addon.get("total")

    packs = []
    for g in _record_grants(session):
        expire = cc.parse_ts(g.get("expiresAt"))
        if g.get("amount") and expire:
            packs.append({"desc": g.get("campaignKey") or "", "credits": g["amount"],
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

    # 历史发放单无 API 可查：按「每日领取 amount、领取后 30 天有效」估算。
    # amount 取当前 campaign 的 benefit；总笔数 = addOnQuota.total / amount。
    if total and (daily := _daily_amount(session)):
        n = round(total / daily)
        if n >= 2:
            first_expire = datetime.now() - timedelta(days=n - 1) + timedelta(days=30)
            est_7d = daily * sum(
                1 for k in range(n - 1)
                if datetime.now() <= first_expire + timedelta(days=k)
                <= datetime.now() + timedelta(days=7))
            result["expiring_7d"] = (result["expiring_7d"] or 0) + est_7d
            result["expiring_7d_estimated"] = True
            result["note"] = (f"历史约 {cc.fmt_num(total - daily)} 积分按日滚动到期"
                              f"（首签估算 {first_expire - timedelta(days=30):%m-%d}，"
                              f"领取后 30 天有效，未记录的每日约 {daily}）")
    return result


def _daily_amount(session: requests.Session):
    doc = get(session, "/sash/api/v1/me/campaigns")
    for c in doc.get("campaigns") or []:
        amount = (c.get("benefit") or {}).get("amount")
        if amount:
            return amount
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="读取 Qoder CN 用量 / 签到")
    ap.add_argument("what", nargs="?", default="usage",
                    help="usage | claim | " + " | ".join(ENDPOINTS))
    ap.add_argument("--days", type=int, help="credits-heatmap 的天数")
    ap.add_argument("--dry-run", action="store_true", help="claim 时只列出将要领取的活动")
    ap.add_argument("--auth-file", help="账号凭证 JSON（对象或对象列表），默认找同目录 auths.json")
    args = ap.parse_args()

    auths = load_auths(args)
    rc = 0
    for idx, auth in enumerate(auths, 1):
        name = (auth.get("user") or {}).get("name") or "?"
        emit(f"===== 账号 {idx}: {name} =====")
        try:
            rc |= run_account(auth, args)
        except Exception as exc:  # 单账号失败不中断其他账号
            print(f"账号 {name} 处理失败: {exc}", file=sys.stderr)
            rc = 1

    # 积分余额与过期（查询失败不影响签到退出码）
    try:
        summary = fetch_credits(make_session(auths[0]["token"])) if auths else {"error": "没有可用登录态"}
    except Exception as exc:
        summary = {"error": str(exc)}
    emit("[CREDITS] " + json.dumps(summary, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
