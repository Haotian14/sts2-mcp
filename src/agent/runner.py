"""整局自动驾驶：读状态 → 策略出决定 → 下发动作，循环到局结束或出故障。

所有决策都在 strategy/ 里，本模块只负责分派、执行、防呆（等待界面到位、
识别「动作下去了局面却没变」），以及把每一局的结果记一行到 logs/runs.jsonl。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .bridge import Bridge
from .gamedata import db
from .strategy import choices, combat, rooms, route
from .strategy.rooms import Decision

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOG_DIR = os.path.join(ROOT, "logs")

WAIT_S = 0.6
MAX_WAITS = 40            # 连续「界面没到位」这么多次才算卡住（终局动画可能很长）
MAX_REPEATS = 4           # 连续这么多步局面指纹不变 → 某个动作静默失效了

CONTINUE_WORDS = ("继续", "continue", "proceed", "确认", "confirm", "ok")

CHARACTER_ALIASES = {
    "ironclad": ("ironclad", "力士"),
    "silent": ("silent", "静默猎手"),
    "defect": ("defect", "故障机器人"),
    "necrobinder": ("necrobinder", "亡灵契约师"),
    "regent": ("regent", "摄政王"),
}


@dataclass
class Memory:
    """跨步骤的少量状态。都按楼层作废，不会串到下一个房间。"""
    floor: int | None = None
    skipped_cards: bool = False
    rerolled: bool = False
    bought: set[str] = field(default_factory=set)
    hint: str | None = None
    run_logged: bool = False

    def at(self, floor: int | None) -> None:
        if floor != self.floor:
            self.floor = floor
            self.skipped_cards = self.rerolled = False
            self.bought = set()


def screen_of(state: dict) -> dict:
    s = state.get("screen")
    return s if isinstance(s, dict) else {}


def decide(state: dict, mem: Memory, new_run: str | None = None) -> Decision:
    """当前局面该做什么。顺序即优先级。"""
    run = state.get("run") or {}
    if run.get("game_over") or not state.get("in_run"):
        if not new_run:
            return Decision("stop", None, "本局已结束" if run.get("game_over") else "不在局中")
        return _menu(state, new_run)

    mem.at(run.get("total_floor"))

    if state.get("awaiting_choice") and state.get("choice"):
        idx, why = choices.answer(state, rooms.deck_context(state), mem.hint)
        mem.hint = None
        return Decision("choose", idx, why)

    if state.get("in_combat"):
        mem.hint = None
        if (state.get("combat") or {}).get("phase") not in (None, "Play"):
            return Decision("wait", None, "不在出牌阶段")
        plan = combat.plan_turn(state)
        if plan.move is None:
            return Decision("end_turn", None, plan.why())
        if "potion" in plan.move:
            return Decision("use_potion", plan.move, plan.why())
        return Decision("play_card", plan.move, plan.why())

    screen = screen_of(state)
    stype = screen.get("type") or ""
    handler: dict[str, Callable[[], Decision]] = {
        "NRewardsScreen": lambda: rooms.rewards(state, mem.skipped_cards),
        "NCardRewardSelectionScreen": lambda: rooms.card_reward(state, mem.rerolled),
        "NRestSiteRoom": lambda: rooms.rest_site(state),
        "NMerchantRoom": lambda: rooms.shop(state, mem.bought),
        "NTreasureRoom": lambda: rooms.treasure(state),
        "NEventRoom": lambda: rooms.event(state),
        "NChooseARelicSelection": lambda: rooms.relic_choice(state),
    }
    if stype in handler:
        d = handler[stype]()
        if d.action == "proceed" or d.action == "wait":
            if (state.get("map") or {}).get("can_move"):
                return _map(state)
        return d

    if (state.get("map") or {}).get("can_move"):
        return _map(state)

    options = [o for o in screen.get("options") or [] if o.get("available") is not False]
    if len(options) == 1:
        return Decision("pick", options[0]["i"], f"{stype} 上唯一的选项")
    for o in options:
        if any(w in str(o.get("id", "")).lower() for w in CONTINUE_WORDS):
            return Decision("pick", o["i"], f"{stype}：继续")
    if screen.get("can_proceed"):
        return Decision("proceed", None, f"{stype}：继续")
    if options:
        return Decision("pick", options[0]["i"], f"不认识的界面 {stype}，点第一个选项")
    return Decision("wait", None, f"停在 {stype or '未知界面'}，等界面到位")


def _map(state: dict) -> Decision:
    i, why = route.best_move(state)
    if i is None:
        return Decision("wait", None, why)
    return Decision("move", i, why)


def _menu(state: dict, character: str) -> Decision:
    """终局 → 主菜单 → 单人 → 标准模式 → 选角色 → 确认。"""
    options = [o for o in screen_of(state).get("options") or [] if o.get("available") is not False]

    def find(*needles: str) -> dict | None:
        keys = [n.lower().replace(" ", "") for n in needles]
        for o in options:
            hay = str(o.get("id") or o.get("title") or "").lower().replace(" ", "").replace("_", "")
            if any(k in hay for k in keys):
                return o
        return None

    if (state.get("run") or {}).get("game_over"):
        o = find("返回主菜单", "mainmenu") or find("继续", "continue")
        return Decision("pick", o["i"], "离开终局界面") if o else Decision("wait", None, "终局动画")

    chars = [o for o in options if "selected" in o]
    if chars:
        names = CHARACTER_ALIASES.get(character.lower(), (character,))
        target = next((o for o in chars if any(n.lower() in str(o.get("id", "")).lower() for n in names)), None)
        if not target:
            return Decision("stop", None, f"角色 {character} 不在可选列表")
        if not target.get("selected"):
            return Decision("pick", target["i"], f"选择角色 {target.get('id')}")
        o = find("确认", "confirm", "embark")
        return Decision("pick", o["i"], "确认开局") if o else Decision("wait", None, "等确认按钮")
    o = find("标准模式", "standard") or find("单人模式", "singleplayer")
    if o:
        return Decision("pick", o["i"], f"菜单：{o.get('id')}")
    return Decision("wait", None, "主菜单切换中")


# --------------------------------------------------------------------------
#  执行
# --------------------------------------------------------------------------


def _signature(state: dict) -> tuple:
    run, player = state.get("run") or {}, state.get("player") or {}
    return (run.get("total_floor"), run.get("gold"), player.get("hp"), player.get("block"),
            screen_of(state).get("type"), len(screen_of(state).get("options") or []),
            len(state.get("hand") or []), (state.get("combat") or {}).get("energy"),
            tuple(e.get("hp") for e in state.get("enemies") or []),
            state.get("awaiting_choice"), (state.get("map") or {}).get("can_move"))


def execute(bridge: Bridge, d: Decision) -> dict:
    a = d.arg
    if d.action == "play_card":
        return bridge.act("play_card", card=a["card"], target=a.get("target"))
    if d.action == "use_potion":
        return bridge.act("use_potion", slot=a["potion"], target=a.get("target"))
    if d.action == "end_turn":
        return bridge.act("end_turn")
    if d.action == "choose":
        return bridge.act("choose", cards=a)
    if d.action == "pick":
        return bridge.act("pick", i=a)
    if d.action == "move":
        return bridge.act("move", node=a)
    if d.action == "proceed":
        return bridge.act("proceed")
    raise ValueError(f"不认识的动作 {d.action}")


def play(bridge: Bridge, max_steps: int = 2000, new_run: str | None = None,
         verbose: Callable[[str], None] | None = None) -> dict[str, Any]:
    """一路打下去，直到局结束（不开新局时）、出故障，或走满 max_steps 步。"""
    mem = Memory()
    state = bridge.state()
    log: list[str] = []
    waits = repeats = 0
    last_sig = None

    def note(line: str) -> None:
        log.append(line)
        if verbose:
            verbose(line)

    for step in range(max_steps):
        _record_run_end(state, mem)
        d = decide(state, mem, new_run)

        if d.action == "stop":
            return _summary(state, log, step, d.why)
        if d.action == "wait":
            waits += 1
            if waits > MAX_WAITS:
                return _summary(state, log, step, f"卡住了：{d.why}")
            time.sleep(WAIT_S)
            state = bridge.state()
            continue
        waits = 0

        sig = _signature(state)
        repeats = repeats + 1 if sig == last_sig else 0
        last_sig = sig
        if repeats >= MAX_REPEATS:
            return _summary(state, log, step, f"连续 {repeats} 步局面没变（上一步 {d.action} {d.arg}），停下")

        floor = (state.get("run") or {}).get("total_floor")
        note(f"[{floor}] {d.action} {d.arg if d.arg is not None else ''} — {d.why}")
        result = execute(bridge, d)
        if d.hint:
            mem.hint = d.hint
        if d.action == "pick" and isinstance(d.arg, int):
            picked = next((o for o in screen_of(state).get("options") or [] if o.get("i") == d.arg), {})
            if picked.get("id") == "Alt:Skip":
                mem.skipped_cards = True
            if picked.get("id") == "Alt:REROLL":
                mem.rerolled = True
        if not result.get("ok"):
            note(f"    被拒绝：{result.get('error')} {result.get('reason') or ''} {result.get('detail') or ''}")
        state = result.get("state") or bridge.state()

    return _summary(state, log, max_steps, f"已走满 {max_steps} 步")


def _summary(state: dict, log: list[str], steps: int, reason: str) -> dict[str, Any]:
    run, player = state.get("run") or {}, state.get("player") or {}
    return {"stopped": reason, "steps": steps,
            "floor": run.get("total_floor"), "act": run.get("act"),
            "hp": f"{player.get('hp')}/{player.get('max_hp')}", "gold": run.get("gold"),
            "deck": [db().name(c) + ("+" if c.endswith("+") else "") for c in state.get("deck") or []],
            "relics": [db().name(r) for r in state.get("relics") or []],
            "log_tail": log[-40:]}


def _record_run_end(state: dict, mem: Memory) -> None:
    run = state.get("run") or {}
    if not run.get("game_over"):
        mem.run_logged = False
        return
    if mem.run_logged:
        return
    mem.run_logged = True
    player = state.get("player") or {}
    line = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "character": player.get("character"),
            "floor": run.get("total_floor"), "act": run.get("act"), "hp": player.get("hp"),
            "ascension": run.get("ascension"), "deck": state.get("deck"), "relics": state.get("relics")}
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "runs.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass
