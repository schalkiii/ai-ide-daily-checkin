# -*- coding: utf-8 -*-
"""飞书群机器人 webhook 推送（可选；未配置 webhook 时自动跳过）。

与 trae-daily-checkin 保持一致：
    webhook 优先级：--feishu <url> > 环境变量 AICHECKIN_FEISHU_WEBHOOK
                    > 同目录 config.json 的 feishuWebhook 字段
    推送通道：curl 主通道（Windows 加 --ssl-no-revoke，规避 schannel 证书
              吊销离线握手失败）+ requests 兜底
    推送失败仅告警，绝不影响签到退出码
"""
import json
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"


def resolve_webhook(cli_value: str | None = None) -> str:
    """按优先级解析 webhook；未配置返回空串（调用方据此跳过推送）。"""
    if cli_value and cli_value.strip():
        return cli_value.strip()
    env = os.environ.get("AICHECKIN_FEISHU_WEBHOOK", "").strip()
    if env:
        return env
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return (cfg.get("feishuWebhook") or "").strip()
    except (OSError, ValueError):
        return ""


def push(webhook: str, text: str | None = None, card: dict | None = None) -> bool:
    """推送消息到飞书群机器人，返回是否送达。任何异常仅告警，不抛出。

    card 非空时发交互卡片（msg_type=interactive），否则发纯文本。
    """
    if not webhook:
        return False
    if card is not None:
        payload = {"msg_type": "interactive", "card": card}
    else:
        payload = {"msg_type": "text", "content": {"text": text or ""}}
    body = json.dumps(payload, ensure_ascii=False)

    # 1) curl 主通道：经 trae-daily-checkin 实战验证可用（Windows curl 不走
    #    环境代理、直连可达；--ssl-no-revoke 规避 schannel 离线吊销检查失败）
    try:
        args = ["curl", "-sS", "-H", "Content-Type: application/json", "-d", body, webhook]
        if sys.platform == "win32":
            args.insert(1, "--ssl-no-revoke")
        r = subprocess.run(args, capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip()
        if r.returncode == 0 and '"code":0' in out.replace(" ", ""):
            print("[INFO] 飞书通知已发送 (curl)")
            return True
        raise RuntimeError(f"curl 退出码 {r.returncode}: {out[:200]}")
    except Exception as e:
        print(f"[WARN] curl 推送失败（{e}），尝试 requests 兜底...", file=sys.stderr)

    # 2) requests 兜底（走 HTTP(S)_PROXY 环境变量）
    try:
        import requests
        resp = requests.post(webhook, json=payload, timeout=10)
        if resp.status_code == 200 and resp.json().get("code") == 0:
            print("[INFO] 飞书通知已发送 (requests)")
            return True
        print(f"[WARN] 飞书通知发送失败：HTTP {resp.status_code} {resp.text[:200]}",
              file=sys.stderr)
    except Exception as e:
        print(f"[WARN] 飞书通知发送失败：{e}", file=sys.stderr)
    return False
