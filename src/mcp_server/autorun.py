"""整局 runner（spec.md 的 6.4）—— 把一整局里**没有决策含量**的步子连起来跑掉。

【它解决的问题】
一局有数百个动作，交互式一步一调，上下文先耗尽、局也没打完（这正是 6.4 的
由来）。但绝大多数步子根本不需要模型：领金币、按继续、地图只有一条路、
常规战斗回合 —— spec §4 的第 1 档。本模块把这些串起来自动做掉，
**一撞上真正的决策就停手交还**。

【与 auto_combat 的关系】
战斗内那段直接委托给 `autoplay.play_combat`，一行都不重写。本模块管的是
战斗**之外**的那一半：奖励界面、地图、宝箱、以及「这个局面我认不认得」。

【默认值是「交还」，不是「猜」】
认不出的界面、多于一条的路、休息点、商店、事件、卡牌三选一，一律停手。
理由见 strategy.md §5：**构筑与路线决策决定一局的上限**，上一局整局的卡牌
奖励都由脚本「拿第一张」，结果是牌组打不动 Boss。省下的那点调用次数，
远不值这个代价。所以这里的原则是：**只做唯一解**，凡是有得选的都交还。
"""

from __future__ import annotations

import time
from typing import Any, Callable

import autoplay
import journal

# 「什么都做不了」时重读几次状态才认定是真卡住（见 play_run）
UNCLEAR_RETRIES = 3

# 战斗奖励里「永远该拿」的那几样（strategy.md §2.2）。
# 卡牌三选一**不在**此列 —— 那是构筑决策，必须交还。
ALWAYS_TAKE = ("GoldReward", "RelicReward", "PotionReward")

# 地图上这两类房间事关全局，绝不本地代拿主意（strategy.md §3）
RISKY_ROOMS = ("Elite", "Boss")


def _screen(state: dict[str, Any]) -> dict[str, Any]:
    return state.get("screen") or {}


def _options(state: dict[str, Any]) -> list[dict[str, Any]]:
    return _screen(state).get("options") or []


def decide(state: dict[str, Any]) -> tuple[str, Any, str]:
    """下一步做什么。返回 `(动作, 参数, 理由)`。

    动作为 `handoff` 时，参数即交还的理由。**顺序即优先级**，越靠前越硬。
    """
    if not state.get("in_run"):
        return "handoff", None, "不在局中（多半停在主菜单）"
    if (state.get("run") or {}).get("game_over"):
        return "handoff", None, "本局已结束"

    # 待答的选择一律交还：`choice` 不区分弃牌 / 检索 / 除卡，而三者的最优
    # 选法互不相同（strategy.md §3）。分不清就不做。
    if state.get("awaiting_choice"):
        return "handoff", None, "游戏正等一次选择（弃牌/检索/除卡），交还"

    if state.get("in_combat"):
        return "combat", None, "战斗交给启发式"

    screen, options = _screen(state), _options(state)
    stype = screen.get("type") or ""

    if options:
        # 宝箱要先开箱才看得见里面的东西，开箱本身没有选择余地
        chest = next((o for o in options if str(o.get("id", "")).lower().startswith("chest")), None)
        if chest:
            return "pick", chest["i"], "开箱（开箱本身没得选）"

        take = next((o for o in options
                     if o.get("id") in ALWAYS_TAKE and o.get("available") is not False), None)
        if take:
            return "pick", take["i"], f"{take['id']} 永远该拿（strategy.md §2.2）"

        # 只剩一个可选项、且它是宝箱里的遗物 —— 遗物同样永远该拿
        usable = [o for o in options if o.get("available") is not False]
        if len(usable) == 1 and "Treasure" in stype:
            return "pick", usable[0]["i"], "宝箱里的遗物，唯一可拿项"

        if screen.get("can_proceed") and not usable:
            return "proceed", None, "界面上已无可领取的东西"

        # 理由里必须点名是什么东西 —— 「有 3 项要挑」不足以让模型知道该干嘛，
        # 而名字（剑柄打击 / 无情猛攻（38金）/ CardReward）一眼就够
        names = "、".join(journal.option_name(options, o["i"]) for o in usable)
        return "handoff", None, f"{stype} 上有需要挑的东西：{names}"

    if screen.get("can_proceed"):
        return "proceed", None, "界面已处理完，按继续"

    map_ = state.get("map") or {}
    if map_.get("can_move"):
        moves = map_.get("options") or []
        risky = [o for o in moves if o.get("type") in RISKY_ROOMS]
        if risky:
            kinds = "、".join(sorted({str(o.get("type")) for o in risky}))
            return "handoff", None, f"下一步可走 {kinds}，路线决策交还（strategy.md §3）"
        if len(moves) == 1:
            return "move", moves[0]["i"], f"地图只有一条路（{moves[0].get('type')}）"
        if len(moves) > 1:
            return "handoff", None, f"地图有 {len(moves)} 条路可选，路线决策交还"
        return "unclear", None, "在地图上但没有可走的节点"

    # 「什么都做不了」有两种可能：真卡住了，或者只是**还没到位**。
    # 二者靠再看一眼分得开，见 play_run 里的重读。
    if stype:
        return "unclear", None, f"停在 {stype} 上，没有可点的选项也不能继续"
    return "unclear", None, "认不出当前局面"


def _signature(state: dict[str, Any]) -> tuple:
    """局面指纹，用来发现「动作下去了，但什么都没变」。"""
    run, player = state.get("run") or {}, state.get("player") or {}
    return (
        run.get("total_floor"), run.get("gold"), player.get("hp"),
        _screen(state).get("type"), len(_options(state)),
        state.get("in_combat"), (state.get("map") or {}).get("can_move"),
    )


def play_run(
    state: dict[str, Any],
    play_card: Callable[[int, int | None], dict[str, Any]],
    end_turn: Callable[[], dict[str, Any]],
    pick: Callable[[int], dict[str, Any]],
    proceed: Callable[[], dict[str, Any]],
    move: Callable[[int], dict[str, Any]],
    max_steps: int = 100,
    max_turns: int = 30,
    on_step: Callable[[dict[str, Any], str, str], None] | None = None,
    refresh: Callable[[], dict[str, Any]] | None = None,
    settle_wait: float = 0.8,
) -> dict[str, Any]:
    """一路跑到需要模型出面为止。

    动作全部由调用方注入，本模块因此不依赖 httpx，可以脱离游戏单测 ——
    与 `autoplay` 同一个理由。
    """
    log: list[dict[str, Any]] = []
    steps = 0
    repeats = 0
    unclear = 0
    last_sig = None

    def note(before: dict[str, Any], label: str, why: str) -> None:
        log.append({"floor": (before.get("run") or {}).get("total_floor"),
                    "do": label, "why": why})
        if on_step:
            try:
                on_step(before, label, why)
            except Exception:      # noqa: BLE001 —— 记日志绝不能打断整局
                pass

    while steps < max_steps:
        action, arg, why = decide(state)

        if action == "handoff":
            return _done(state, log, steps, "handoff", why)

        # 「什么都做不了」先当成**还没到位**，隔一会儿再看一眼。
        # 实机撞到（2026-08-01，第 11 层）：一场战斗打完，奖励界面还没浮出来，
        # 此刻的局面是 `NCombatRoom` + 零选项 + 不能继续 —— 照字面判就是
        # 「卡住了，交还」，而 0.8 秒后奖励界面就到了。
        # 这与 3.4c 那个「等待判据早于目标事件」是同一类错误。
        if action == "unclear":
            if refresh and unclear < UNCLEAR_RETRIES:
                unclear += 1
                time.sleep(settle_wait)
                state = refresh() or state
                continue
            return _done(state, log, steps, "handoff",
                         f"{why} —— 等了 {unclear} 次仍是如此，需要人工看一眼")
        unclear = 0

        # 「动作下去了但局面没变」必须当成故障停下来。不然一旦某个点击静默
        # 失效（3.4f 的商店槽位就是这样，ForceClick 被 _isHovered 挡掉、
        # 不报错也不改状态），runner 会以每秒数次的速度空转到 max_steps。
        sig = _signature(state)
        repeats = repeats + 1 if sig == last_sig else 0
        last_sig = sig
        if repeats >= 3:
            return _done(state, log, steps, "stuck",
                         f"连续 {repeats} 步局面毫无变化（上一步：{action} {arg}）—— "
                         f"多半是某个动作静默失效了，停下来看一眼")

        if action == "combat":
            result = autoplay.play_combat(state, play_card, end_turn,
                                          max_turns=max_turns, on_step=on_step)
            log.extend({"floor": (state.get("run") or {}).get("total_floor"),
                        "do": e["play"], "why": e["why"]} for e in result["log"])
            state = result.get("state") or state
            steps += 1
            if result["stopped"] != "combat_ended":
                # 战斗内的交还就是整局的交还 —— 局面原样留给模型
                return _done(state, log, steps, result["stopped"], result["reason"])
            continue

        before = state
        # 名字要在下发**之前**取 —— 领完金币那个选项当场就从界面上消失了
        if action == "pick":
            label = f"pick {journal.option_name(_options(state), arg)}"
            result = pick(arg)
        elif action == "proceed":
            label = "proceed"
            result = proceed()
        elif action == "move":
            label = f"move → {journal.option_name((state.get('map') or {}).get('options'), arg)}"
            result = move(arg)
        else:
            return _done(state, log, steps, "handoff", f"不认识的动作：{action}")

        if not result.get("ok"):
            return _done(result.get("state") or state, log, steps, "rejected",
                         f"{label} 被拒绝（{result.get('error')}：{result.get('reason')}）—— "
                         f"runner 与游戏的判断不一致，交还")

        note(before, label, why)
        state = result.get("state") or state
        steps += 1

    return _done(state, log, steps, "max_steps", f"已走满 {max_steps} 步，交还")


def _done(state: dict[str, Any], log: list[dict[str, Any]], steps: int,
          stopped: str, reason: str) -> dict[str, Any]:
    return {"ok": True, "stopped": stopped, "reason": reason,
            "steps": steps, "log": log, "state": state}
