"""选牌应答：游戏停下来要玩家挑牌时（/state 的 awaiting_choice + choice）。

同一个「从 N 张里挑 k 张」，按用途最优解**正好相反**：
- 弃牌 / 消耗 / 除卡 / 变化 → 挑最没用的；
- 升级 → 挑升级收益最大的；
- 检索 / 保留 / 获得 → 挑最有用的。

用途来自桥接层的 `choice.purpose`（CardSelectCmd 的入口名）与 `choice.source`
（发起选择的牌/遗物/事件）；两者都拿不到时，看候选牌是不是来自自己的牌库。
"""

from __future__ import annotations

from collections import Counter

from ..gamedata import base_id, db
from . import combat
from . import deck as deckval
from .deck import DeckContext

NEGATIVE = "negative"
UPGRADE = "upgrade"
POSITIVE = "positive"


def purpose_of(choice: dict, state: dict, hint: str | None = None) -> str:
    purpose = (choice.get("purpose") or "").lower()
    source = choice.get("source") or ""
    if hint in (NEGATIVE, UPGRADE, POSITIVE):
        return hint
    if "upgrade" in purpose:
        return UPGRADE
    if "discard" in purpose or "transform" in purpose or "removal" in purpose:
        return NEGATIVE
    if "reward" in purpose or "chooseacard" in purpose or "bundle" in purpose:
        return POSITIVE

    # 牌自己的效果里写着用途（extract_gamedata 从 OnPlay 抽出的 choose.kind）
    kinds = {e.get("kind") for e in (db().card(source).get("effects") or []) if e.get("op") == "choose"}
    if kinds & {"discard", "exhaust"}:
        return NEGATIVE
    if "upgrade" in kinds:
        return UPGRADE
    if kinds & {"fetch", "select"}:
        return POSITIVE

    # 兜底：候选全是自己牌库里的牌 → 多半是除卡/变化；否则是「获得」
    deck = {base_id(c) for c in state.get("deck") or []}
    ids = [o.get("id") for o in choice.get("options") or []]
    if ids and all(i in deck for i in ids) and not state.get("in_combat"):
        return NEGATIVE
    return POSITIVE


def answer(state: dict, ctx: DeckContext, hint: str | None = None) -> tuple[list[int], str]:
    choice = state.get("choice") or {}
    options = choice.get("options") or []
    lo, hi = choice.get("min") or 0, choice.get("max") or 0
    purpose = purpose_of(choice, state, hint)

    def cid(o: dict) -> str:
        return o.get("id", "") + ("+" if o.get("upgraded") else "")

    if purpose == UPGRADE:
        ranked = sorted(options, key=lambda o: deckval.upgrade_gain(cid(o))
                        + 0.4 * deckval.value_in_deck(cid(o), ctx), reverse=True)
        n = hi
    elif purpose == NEGATIVE:
        in_combat = bool(state.get("in_combat"))
        planned = _planned_cards(state) if in_combat else Counter()
        ranked = sorted(options, key=lambda o: _discard_key(o, in_combat, ctx, planned))
        n = lo
    else:
        ranked = sorted(options, key=lambda o: deckval.value_in_deck(cid(o), ctx), reverse=True)
        n = hi if hi else lo
        # 「加进牌组」可以少拿：分数低于门槛的不要（但至少满足下限）。
        # 战斗中的检索/回手是白拿，不稀释牌组，一律拿满。
        if not state.get("in_combat"):
            n = max(lo, sum(1 for o in ranked[:n]
                            if deckval.value_in_deck(cid(o), ctx) >= deckval.skip_threshold(ctx)))

    picked = ranked[:max(lo, min(n, hi if hi else n))]
    names = "、".join(db().name(o.get("id", "")) for o in picked) or "（不选）"
    label = {UPGRADE: "升级", NEGATIVE: "丢掉", POSITIVE: "拿"}[purpose]
    return [o["i"] for o in picked], f"{label} {names}（{choice.get('purpose') or '?'} / {choice.get('source') or '?'}）"


def _planned_cards(state: dict) -> Counter:
    """战斗规划这回合打算打出的手牌（按 id 计数）。规划失败时返回空，退回纯估值。"""
    try:
        return Counter(combat.plan_turn(state).used)
    except Exception:  # 规划只是参考，任何异常都不该让选牌卡住
        return Counter()


def _discard_key(o: dict, in_combat: bool, ctx: DeckContext, planned: Counter | None = None) -> float:
    d = db().card(o.get("id", ""))
    kws = d.get("keywords") or []
    value = deckval.value_in_deck(o.get("id", "") + ("+" if o.get("upgraded") else ""), ctx)
    if in_combat:
        if "Sly" in kws:
            return -10.0                          # 奇巧：弃掉 = 免费打出
        if d.get("type") in ("Curse", "Status"):
            # 虚无的诅咒留在手里，回合末自己消耗；弃掉反而会再抽到
            return 3.0 if "Ethereal" in kws else -8.0
        # 这回合要打的牌留着，打不上的先弃。静态估值只排同一类里的先后 ——
        # 实战里投掷匕首弃掉过唯一的致命毒药，敌人攻击时也弃过防御
        if planned and planned[o.get("id", "")] > 0:
            planned[o.get("id", "")] -= 1
            return 5.0 + value
    return value
