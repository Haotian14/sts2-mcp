"""直连桥接层跑一遍战斗启发式 —— 开发期验证用，不经 MCP。

【为什么需要它】
启发式住在 MCP server 里，而 MCP 工具列表在会话启动时就固定了：改完
`autoplay.py` 得重启整个 Claude Code 会话，新工具才会出现。开发期改一版验一版
根本等不起。本脚本直接拿 `/state` 喂给同一份 `autoplay`，走的是同样的代码路径，
只是绕过了 MCP 那一层壳。

用法：
    python scripts/autocombat.py            # 打完当前这场
    python scripts/autocombat.py --turns 3  # 最多打 3 个回合
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "mcp_server"))
import autoplay  # noqa: E402
import journal  # noqa: E402

BRIDGE = os.environ.get("STS2MCP_URL", "http://127.0.0.1:8765").rstrip("/")


def _call(method: str, path: str, params: dict | None = None) -> dict:
    url = f"{BRIDGE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    # end_turn 要等完整个敌方回合，实测 5~8 秒；给足余量
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=20)
    args = parser.parse_args()

    state = _call("GET", "/state")
    result = autoplay.play_combat(
        state,
        play_card=lambda card, target: _call(
            "POST", "/action/play_card",
            {"card": card, **({"target": target} if target is not None else {})},
        ),
        end_turn=lambda: _call("POST", "/action/end_turn"),
        max_turns=args.turns,
        # 与 MCP 那条路走同一份决策日志（6.5）：开发期跑出来的战斗同样进日志，
        # 否则「跨局累积」会漏掉所有用本脚本打的场次
        on_step=lambda before, label, why: journal.record(before, label, why, by="heuristic"),
    )
    journal.record(result.get("state") or {},
                   f"auto_combat 停手（{result['stopped']}）", result["reason"],
                   by="heuristic", kind="stop")

    for entry in result["log"]:
        print(f"  回合{entry['turn']}  {entry['play']:<34} {entry['why']}")

    player = (result["state"].get("player") or {})
    print(f"\n停手：{result['stopped']}  —— {result['reason']}")
    print(f"回合数 {result['turns']}   HP {player.get('hp')}/{player.get('max_hp')}")
    print(f"决策日志 → {journal.PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
