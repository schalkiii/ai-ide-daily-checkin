# -*- coding: utf-8 -*-
"""积分汇总的共享工具：时间解析/格式化、临期合计、数字格式化。

三家提供商的过期时间表示各异（Trae=epoch 秒、WorkBuddy="YYYY-MM-DD HH:MM:SS"、
Qoder=ISO8601 Z），统一规范成 datetime 后交由本模块处理。
"""
from datetime import datetime, timedelta


def parse_ts(value) -> datetime | None:
    """把各家的过期时间表示解析为本地 datetime；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value)
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text.replace("Z", "+0000") if "%z" in fmt else text, fmt)
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            continue
    return None


def fmt_dt(dt: datetime | None) -> str:
    """MM-DD HH:MM；跨年时带年份。None -> '-'。"""
    if dt is None:
        return "-"
    prefix = f"{dt.year}-" if dt.year != datetime.now().year else ""
    return f"{prefix}{dt:%m-%d %H:%M}"


def sum_expiring(packs: list, days: int, now: datetime | None = None) -> float:
    """days 天内到期的积分合计。packs 元素需含 expire_dt(datetime) 与 credits。"""
    now = now or datetime.now()
    deadline = now + timedelta(days=days)
    return sum(p.get("credits") or 0 for p in packs
               if p.get("expire_dt") and now <= p["expire_dt"] <= deadline)


def fmt_num(value) -> str:
    """积分数字格式化：整数不带小数点，小数保留 1 位。None -> '?'。"""
    if value is None:
        return "?"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.1f}"
    return str(int(value))
