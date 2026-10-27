# -*- coding: utf-8 -*-
"""统一签到入口：按参数调度各家签到脚本，并汇总推送飞书（可选）。

    python checkin.py all            # 依次跑 Qoder CN / Trae CN / WorkBuddy / 豆包（库库暂不参与）
    python checkin.py qoder claim    # 只跑 Qoder，并把参数透传给 checkin_qoder.py
    python checkin.py trae
    python checkin.py workbuddy
    python checkin.py kuku           # 库库脱机接口已失效，保留手动入口；日常由姊妹 CDP 项目负责
    python checkin.py doubao
    python checkin.py all --dry-run  # --dry-run 只透传给支持的脚本（Qoder / 库库 / 豆包），其余忽略
    python checkin.py all --no-push  # 只签到、不推送飞书（测试用）
    python checkin.py all --feishu <url>  # 临时指定飞书 webhook（覆盖 config.json）

各家互相独立：某个客户端未安装/未登录只会让该家失败，不影响其余。
最终退出码 = 所有已尝试脚本退出码的按位或（0 表示全部成功）。
计划任务里用一条 `pythonw checkin.py all` 即可覆盖当前启用的几家；库库暂不参与，
由姊妹 CDP 项目 `kuku-ai-daily-checkin` 负责。

飞书推送（feishu_notify.py）：跑完汇总各家状态发一条群机器人交互卡片
    （卡片发送失败自动回退纯文本）。内容含「积分与过期」小节：各家余额、
    最近到期时间与数量、7 天内到期合计（数据由各 checkin_<name>.py 的
    fetch_credits() 以 [CREDITS] 行输出）。
    webhook 优先级：--feishu <url> > 环境变量 AICHECKIN_FEISHU_WEBHOOK
                    > 同目录 config.json 的 feishuWebhook 字段
推送失败仅告警，不影响签到退出码。
"""
import json
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
DRY_RUN_CAPABLE = {"qoder", "kuku", "doubao"}
SCRIPTS = {
    "qoder": ("checkin_qoder.py", []),
    "trae": ("checkin_trae.py", []),
    "workbuddy": ("checkin_workbuddy.py", []),
    "kuku": ("checkin_kuku.py", []),
    "doubao": ("checkin_doubao.py", []),
}
# 库库脱机接口已失效（2026-10 活动改用 App 注入的 jsToken），暂不参与 all；
# 需要恢复时把 "kuku" 加回，或直接 `checkin.py kuku` 手动跑。
ALL_ORDER = ["qoder", "trae", "workbuddy", "doubao"]
DISPLAY_NAMES = {
    "qoder": "Qoder CN",
    "trae": "Trae CN",
    "workbuddy": "WorkBuddy",
    "kuku": "库库AI",
    "doubao": "豆包",
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
    r = subprocess.run(cmd, capture_output=True, text=True, check=False,
                       encoding="utf-8", errors="replace")
    if r.stdout:
        print(r.stdout, end="")
    if r.stderr:
        print(r.stderr, end="", file=sys.stderr)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def detail_line(output: str) -> str:
    """取子脚本输出里最后一条关键行（去掉日志时间前缀）作为推送详情。"""
    lines = [LOG_PREFIX.sub("", line.strip()) for line in output.splitlines()]
    lines = [line for line in lines if line and not CREDITS_LINE.search(line)]
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


def md_escape(text: str) -> str:
    """只替换 <，避免日志内容被 lark_md 当成标签；保留 > 与 & 原样显示。"""
    return str(text).replace("<", "＜")


def card_credits_line(name: str, summary: dict) -> str:
    """卡片中的单家积分块：余额/到期信息一行，估算备注单独灰色一行。"""
    if "error" in summary:
        return (f"**{DISPLAY_NAMES[name]}**　<font color='red'>查询失败</font>\n"
                f"<font color='grey'>{md_escape(str(summary['error'])[:80])}</font>")
    parts = [f"余额 **{cc.fmt_num(summary.get('remaining'))}**"]
    nearest = summary.get("nearest")
    if nearest:
        parts.append(f"最近到期 **{nearest['expire']}**（{cc.fmt_num(nearest['credits'])}）")
    expiring = summary.get("expiring_7d")
    if expiring:
        est = "~" if summary.get("expiring_7d_estimated") else ""
        parts.append(f"7 天内到期 **{est}{cc.fmt_num(expiring)}**")
    line = f"**{DISPLAY_NAMES[name]}**　" + "　|　".join(parts)
    if summary.get("note"):
        line += f"\n<font color='grey'>{md_escape(summary['note'])}</font>"
    return line


def build_card(results: list[tuple[str, int, str]], rc_total: int,
               credits: dict | None = None) -> dict:
    """组装飞书交互卡片：成功率着色标题 + 每家状态 + 积分小节。"""
    ok_count = sum(1 for _, rc, _ in results if rc == 0)
    total = len(results)
    template = "green" if ok_count == total else ("red" if ok_count == 0 else "orange")
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": (
            f"**成功** {ok_count}/{total} 家　|　**聚合退出码** {rc_total}\n"
            f"<font color='grey'>{datetime.now():%Y-%m-%d %H:%M:%S}</font>")}},
        {"tag": "hr"},
    ]
    for name, rc, output in results:
        status = ("<font color='green'>成功</font>" if rc == 0
                  else "<font color='red'>失败</font>")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": (
            f"**{DISPLAY_NAMES[name]}**　{status}\n"
            f"<font color='grey'>{md_escape(detail_line(output))}</font>")}})
    credit_entries = [(n, s) for n, s in (credits or {}).items() if s]
    if credit_entries:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": "**积分与过期**"}})
        elements.extend(
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": card_credits_line(name, summary)}}
            for name, summary in credit_entries)
    elements.append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": "自动签到 · 数据来自本机客户端登录态"}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text",
                      "content": f"AI IDE 每日签到 · {ok_count}/{total} 成功"},
        },
        "elements": elements,
    }


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0

    webhook_arg, no_push, rest = extract_push_args(argv)
    target, *passthrough = rest
    if target == "all":
        order = ALL_ORDER
    elif target in SCRIPTS:
        order = [target]
    else:
        # 未知目标：当作全部，并把该 token 一并透传（容错）
        log(f"未识别目标 '{target}'，按 all 处理；额外参数透传。")
        order, passthrough = ALL_ORDER, rest

    results: list[tuple[str, int, str]] = []
    credits: dict[str, dict] = {}
    rc = 0
    for name in order:
        # --dry-run 仅 Qoder / 库库 / 豆包 支持；对其余家过滤掉，避免 argparse 报错
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
            text = build_message(results, rc, credits)
            card = build_card(results, rc, credits)
            sent = feishu_notify.push(webhook, text, card=card)
            log("飞书推送：" + ("已发送" if sent else "失败（详见上方告警）"))
        else:
            log("未配置飞书 webhook（config.json feishuWebhook / AICHECKIN_FEISHU_WEBHOOK / --feishu），跳过推送")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
