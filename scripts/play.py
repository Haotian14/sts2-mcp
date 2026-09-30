"""不经 MCP，直接在命令行跑自动驾驶（开发期看它每一步在想什么）。

用法：
    python scripts/play.py                  # 打到本局结束
    python scripts/play.py --steps 30       # 只走 30 步
    python scripts/play.py --advise         # 只看建议，不执行
    python scripts/play.py --new-run silent # 一局结束后自动开下一局
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent import runner  # noqa: E402
from agent.bridge import Bridge  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=2000)
    ap.add_argument("--advise", action="store_true")
    ap.add_argument("--new-run", default=None, help="不在局中/局结束时开新局用的角色")
    ap.add_argument("--runs", type=int, default=None, help="打满几局就停")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    bridge = Bridge()
    if args.advise:
        d = runner.decide(bridge.state(), runner.Memory())
        print(d.action, d.arg, "—", d.why)
        return
    summary = runner.play(bridge, max_steps=args.steps, new_run=args.new_run, verbose=print,
                          max_runs=args.runs)
    summary.pop("log_tail", None)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
