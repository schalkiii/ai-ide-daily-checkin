# -*- coding: utf-8 -*-
"""统一签到入口：按参数调度四个产品的签到脚本，并汇总推送飞书（可选）。

    python checkin.py all            # 依次跑 Qoder CN / Trae CN / WorkBuddy / 库库AI
    python checkin.py qoder claim    # 只跑 Qoder，并把参数透传给 checkin_qoder.py
    python checkin.py trae
    python checkin.py workbuddy
    python checkin.py kuku
    python checkin.py all --dry-run  # --dry-run 只透传给支持的脚本（Qoder / 库库），其余忽略
    python checkin.py all --no-push  # 只签到、不推送飞书（测试用）
    python checkin.py all --feishu <url>  # 临时指定飞书 webhook（覆盖 config.json）

四家互相独立：某个客户端未安装/未登录只会让该家失败，不影响其余。
最终退出码 = 所有已尝试脚本退出码的按位或（0 表示全部成功）。
计划任务里用一条 `pythonw checkin.py all` 即可覆盖四家。

飞书推送（feishu_notify.py）：跑完汇总各家状态发一条群机器人消息。
    推送内容含「积分与过期」小节：各家余额、最近到期时间与数量、7 天内到期合计
    （数据由各 checkin_<name>.py 的 fetch_credits() 以 [CREDITS] 行输出）。
    webhook 优先级：--feishu <url> > 环境变量 AICHECKIN_FEISHU_WEBHOOK
                    > 同目录 config.json 的 feishuWebhook 字段
推送失败仅告警，不影响签到退出码。
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import credits_common as cc
import feishu_notify

BASE = Path(__file__).parent
LOG_FILE = BASE / "checkin.log"
# 名称 -> (脚本文件, 该脚本自身接受的默认参数)
# 支持 --dry-run 的脚本；其余家传入 --dry-run 会被 argparse 拒绝，故在下面过滤掉
DRY_RUN_CAPABLE = {"qoder", "kuku"}
SCRIPTS = {
    "qoder": ("checkin_qoder.py", []),
    "trae": ("checkin_trae.py", []),
    "workbuddy": ("checkin_workbuddy.py", []),
    "kuku": ("checkin_kuku.py", []),
}
DISPLAY_NAMES = {
    "qoder": "Qoder CN",
    "trae": "Trae CN",
    "workbuddy": "WorkBuddy",
    "kuku": "库库AI",
}
# 从子脚本输出中挑“关键行”做推送详情（命中任一关键词的行，取最后一条）
DETAIL_KEYWORDS = ("签到", "已签", "领取", "积分", "ERROR", "失败", "过期",
                   "无效", "没有", "超时", "跳过", "活动")
# 子脚本输出的积分汇总行：[CREDITS] {json}
CREDITS_LINE = re.compile(r"\[CREDITS\]\s*(\{.*\})\s*$")
LOG_PREFIX = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def log(msg: str) -> None:
    """汇总层自己的日志：pythonw 丢弃 stdout，这里显式落 checkin.log 便于排查。"""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def extract_push_args(argv: list[str]) -> tuple[str | None, bool, list[str]]:
    """从参数中抽出 --feishu <url> / --no-push，其余原样留给各子脚本。"""
    webhook, no_push, rest = None, False, []
    it = iter(argv)
    for arg in it:
        if arg == "--no-push":
            no_push = True
        elif arg == "--feishu":
            webhook = next(it, "")
        elif arg.startswith("--feishu="):
            webhook = arg.split("=", 1)[1]
        else:
            rest.append(arg)
    return webhook, no_push, rest


def run(name: str, extra_args: list[str]) -> tuple[int, str]:
    """跑单个子脚本：捕获输出回显，返回 (退出码, 输出文本)。"""
    script, defaults = SCRIPTS[name]
    path = BASE / script
    if not path.exists():
        log(f"[{name}] 跳过：找不到 {script}")
        return 1, ""
    cmd = [sys.executable, str(path), *defaults, *extra_args]
    print(f"\n========== 运行 {name} :: {' '.join(cmd[1:])} ==========")
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.stdout:
        print(r.stdout, end="")
    if r.stderr:
        print(r.stderr, end="", file=sys.stderr)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def detail_line(output: str) -> str:
    """取子脚本输出里最后一条关键行（去掉日志时间前缀）作为推送详情。"""
    lines = [LOG_PREFIX.sub("", line.strip()) for line in output.splitlines()]
    lines = [line for line in lines if line]
    hits = [line for line in lines if any(k in line for k in DETAIL_KEYWORDS)]
    return (hits[-1] if hits else (lines[-1] if lines else "无输出"))[:100]


def extract_credits(output: str) -> dict | None:
    """从子脚本输出中取最后一条 [CREDITS] JSON 行（无则返回 None）。"""
    summary = None
    for line in output.splitlines():
        m = CREDITS_LINE.search(LOG_PREFIX.sub("", line))
        if m:
            try:
                summary = json.loads(m.group(1))
            except ValueError:
                pass  # 容忍单行截断，继续找更早的有效行
    return summary


def credits_line(name: str, summary: dict) -> str:
    """渲染一家的积分小结：余额｜最近到期（数量）｜7天内到期（+备注/错误）。"""
    if "error" in summary:
        return f"{DISPLAY_NAMES[name]}：查询失败（{str(summary['error'])[:40]}）"
    parts = [f"余额 {cc.fmt_num(summary.get('remaining'))}"]
    nearest = summary.get("nearest")
    if nearest:
        parts.append(f"最近到期 {nearest['expire']}（{cc.fmt_num(nearest['credits'])}）")
    expiring = summary.get("expiring_7d")
    if expiring:
        est = "~" if summary.get("expiring_7d_estimated") else ""
        parts.append(f"7天内到期 {est}{cc.fmt_num(expiring)}")
    if summary.get("note"):
        parts.append(summary["note"])
    return f"{DISPLAY_NAMES[name]}：" + "｜".join(parts)


def build_message(results: list[tuple[str, int, str]], rc_total: int,
                  credits: dict | None = None) -> str:
    """组装飞书文本：每家一行签到状态+详情，随后积分小节，末尾时间与汇总。"""
    ok_count = sum(1 for _, rc, _ in results if rc == 0)
    body = ["【AI IDE 每日签到】"]
    for name, rc, output in results:
        status = "成功" if rc == 0 else "失败"
        body.append(f"{DISPLAY_NAMES[name]}：{status}（{detail_line(output)}）")
    credit_entries = [(n, s) for n, s in (credits or {}).items() if s]
    if credit_entries:
        body.append("── 积分与过期 ──")
        body.extend(credits_line(name, summary) for name, summary in credit_entries)
    body.append(f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    body.append(f"汇总：{ok_count}/{len(results)} 家成功，聚合退出码 {rc_total}")
    return "\n".join(body)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0

    webhook_arg, no_push, rest = extract_push_args(argv)
    target, *passthrough = rest
    if target == "all":
        order = ["qoder", "trae", "workbuddy", "kuku"]
    elif target in SCRIPTS:
        order = [target]
    else:
        # 未知目标：当作全部，并把该 token 一并透传（容错）
        log(f"未识别目标 '{target}'，按 all 处理；额外参数透传。")
        order, passthrough = ["qoder", "trae", "workbuddy", "kuku"], rest

    results: list[tuple[str, int, str]] = []
    credits: dict[str, dict] = {}
    rc = 0
    for name in order:
        # --dry-run 仅 Qoder / 库库 支持；对其余家过滤掉，避免 argparse 报错
        args = [a for a in passthrough if not (name not in DRY_RUN_CAPABLE and a == "--dry-run")]
        child_rc, output = run(name, args)
        results.append((name, child_rc, output))
        summary = extract_credits(output)
        if summary:
            credits[name] = summary
        rc |= child_rc

    log(f"全部完成，聚合退出码 = {rc}")

    # 汇总推送飞书（未配置 webhook 或 --no-push 时跳过；推送失败不影响退出码）
    if not no_push:
        webhook = feishu_notify.resolve_webhook(webhook_arg)
        if webhook:
            sent = feishu_notify.push(webhook, build_message(results, rc, credits))
            log("飞书推送：" + ("已发送" if sent else "失败（详见上方告警）"))
        else:
            log("未配置飞书 webhook（config.json feishuWebhook / AICHECKIN_FEISHU_WEBHOOK / --feishu），跳过推送")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
