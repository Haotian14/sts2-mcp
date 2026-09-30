"""MCP 入口。策略全部在代码里（strategy/），这里只暴露几个薄工具：

- play    自动打，直到本局结束 / 出故障 / 走满步数；返回摘要
- step    只走一步（看它在干什么）
- advise  只给建议不执行（策略此刻会怎么做、为什么）
- get_state / act / health  手动查看与干预

运行：python -m agent.server（工作目录 src/），或见仓库根目录的 .mcp.json。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "agent"

from mcp.server.mcpserver import MCPServer  # noqa: E402

from agent import runner  # noqa: E402
from agent.bridge import Bridge  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

bridge = Bridge()

server = MCPServer(
    name="sts2",
    instructions=(
        "《杀戮尖塔 2》单机自动游玩。策略写在代码里：通常只需要调用 play。\n"
        "想看它每一步在想什么用 advise / step；需要手动干预时用 act。\n"
        "仅用于单机离线游玩，不得用于多人模式。"
    ),
)


@server.tool(description=(
    "自动游玩，直到本局结束、出现故障，或走满 max_steps 步。"
    "new_run_character（如 silent）不为空时，本局结束后自动开下一局并继续。"
    "返回停下的原因、层数、血量、牌组、遗物与最后 40 条决策。"
))
def play(max_steps: int = 2000, new_run_character: str = "") -> dict[str, Any]:
    return runner.play(bridge, max_steps=max_steps, new_run=new_run_character or None)


@server.tool(description="按策略走一步，返回做了什么、为什么，以及执行结果。")
def step() -> dict[str, Any]:
    state = bridge.state()
    d = runner.decide(state, runner.Memory())
    if d.action in ("stop", "wait"):
        return {"action": d.action, "why": d.why}
    result = runner.execute(bridge, d)
    return {"action": d.action, "arg": d.arg, "why": d.why, "ok": result.get("ok"),
            "error": result.get("error"), "reason": result.get("reason")}


@server.tool(description="策略此刻会怎么做、为什么（不执行）。")
def advise() -> dict[str, Any]:
    d = runner.decide(bridge.state(), runner.Memory())
    return {"action": d.action, "arg": d.arg, "why": d.why}


@server.tool(description="读取完整的游戏状态（桥接层 /state 原样返回）。")
def get_state() -> dict[str, Any]:
    return bridge.state()


@server.tool(description=(
    "手动下发一个桥接层动作。action 取 play_card(card,target) / end_turn / "
    "use_potion(slot,target) / choose(cards) / pick(i) / proceed / move(node) / resume_run。"
    "下标与 get_state 严格对应。"
))
def act(action: str, card: int | None = None, target: int | None = None, slot: int | None = None,
        i: int | None = None, node: int | None = None, cards: list[int] | None = None) -> dict[str, Any]:
    return bridge.act(action, card=card, target=target, slot=slot, i=i, node=node, cards=cards)


@server.tool(description="桥接层是否在线。")
def health() -> dict[str, Any]:
    return bridge.health()


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
