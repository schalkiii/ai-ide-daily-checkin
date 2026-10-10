# -*- coding: utf-8 -*-
"""统一签到入口：按参数调度三个产品的签到脚本。

    python checkin.py all            # 依次跑 Qoder CN / Trae CN / WorkBuddy
    python checkin.py qoder claim    # 只跑 Qoder，并把参数透传给 checkin_qoder.py
    python checkin.py trae
    python checkin.py workbuddy
    python checkin.py all --dry-run  # --dry-run 只对支持的脚本（Qoder）生效，其余忽略

三家互相独立：某个客户端未安装/未登录只会让该家失败，不影响其余。
最终退出码 = 所有已尝试脚本退出码的按位或（0 表示全部成功）。
计划任务里用一条 `pythonw checkin.py all` 即可覆盖三家。
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
# 名称 -> (脚本文件, 该脚本自身接受的默认参数)
SCRIPTS = {
    "qoder": ("checkin_qoder.py", []),
    "trae": ("checkin_trae.py", []),
    "workbuddy": ("checkin_workbuddy.py", []),
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def run(name: str, extra_args: list[str]) -> int:
    script, defaults = SCRIPTS[name]
    path = os.path.join(BASE, script)
    if not os.path.exists(path):
        print(f"[{name}] 跳过：找不到 {script}", file=sys.stderr)
        return 1
    cmd = [sys.executable, path, *defaults, *extra_args]
    print(f"\n========== 运行 {name} :: {' '.join(cmd[1:])} ==========")
    return subprocess.call(cmd)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0

    target, *rest = argv
    if target == "all":
        order = ["qoder", "trae", "workbuddy"]
    elif target in SCRIPTS:
        order = [target]
    else:
        # 未知目标：当作全部，并把该 token 一并透传（容错）
        print(f"未识别目标 '{target}'，按 all 处理；额外参数透传。", file=sys.stderr)
        order, rest = ["qoder", "trae", "workbuddy"], argv

    rc = 0
    for name in order:
        # --dry-run 仅 Qoder 支持；对 Trae / WorkBuddy 过滤掉，避免 argparse 报错
        args = [a for a in rest if not (name != "qoder" and a == "--dry-run")]
        rc |= run(name, args)
    print(f"\n全部完成，聚合退出码 = {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
