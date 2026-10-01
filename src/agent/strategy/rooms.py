"""战斗以外的界面：奖励、卡牌三选一、休息点、商店、宝箱、事件、遗物多选一。

每个函数吃 /state、返回一个 Decision。只做决定，不碰桥接层 —— 执行在 runner。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..gamedata import db
from . import deck as deckval
from . import route
from .deck import DeckContext


@dataclass
class Decision:
    action: str                 # pick / proceed / move / choose / wait
    arg: int | list[int] | None
    why: str
    hint: str | None = None     # 这一步之后若弹出选牌，用途是什么（见 choices.purpose_of）


def deck_context(state: dict) -> DeckContext:
    run = state.get("run") or {}
    return DeckContext(character=(state.get("player") or {}).get("character") or "",
                       deck=list(state.get("deck") or []), act=run.get("act") or 1,
                       floor=run.get("total_floor") or 0, relics=list(state.get("relics") or []))


def _options(state: dict) -> list[dict]:
    return [o for o in (state.get("screen") or {}).get("options") or [] if o.get("available") is not False]


def _find(options: list[dict], oid: str) -> dict | None:
    return next((o for o in options if o.get("id") == oid), None)


def _proceed_or_wait(state: dict, why: str) -> Decision:
    if (state.get("screen") or {}).get("can_proceed"):
        return Decision("proceed", None, why)
    return Decision("wait", None, "界面还没到位")


# --------------------------------------------------------------------------
#  战后奖励
# --------------------------------------------------------------------------


def rewards(state: dict, skipped_cards: bool) -> Decision:
    options = _options(state)
    potions = state.get("potions") or []
    free_slot = any(p is None for p in potions)
    for kind in ("GoldReward", "RelicReward"):
        o = _find(options, kind)
        if o:
            return Decision("pick", o["i"], f"领取 {kind}")
    o = _find(options, "PotionReward")
    if o and free_slot:
        return Decision("pick", o["i"], "领取药水")
    o = _find(options, "CardReward")
    if o and not skipped_cards:
        return Decision("pick", o["i"], "打开卡牌奖励")
    # 其余奖励（特殊卡牌奖励等）一律领取
    for o in options:
        if o.get("id") not in ("PotionReward", "CardReward"):
            return Decision("pick", o["i"], f"领取 {o.get('id')}")
    return _proceed_or_wait(state, "奖励处理完毕")


def card_reward(state: dict, rerolled: bool) -> Decision:
    options = _options(state)
    cards = [o for o in options if not str(o.get("id", "")).startswith("Alt:")]
    ctx = deck_context(state)
    best, _, why = deckval.pick_card([o["id"] for o in cards], ctx)
    if best:
        o = next(o for o in cards if o["id"] == best)
        return Decision("pick", o["i"], why)
    reroll = _find(options, "Alt:REROLL")
    if reroll and not rerolled:
        return Decision("pick", reroll["i"], why + "，先重抽一次")
    skip = _find(options, "Alt:Skip")
    if skip:
        return Decision("pick", skip["i"], why)
    return _proceed_or_wait(state, why + "（没有跳过按钮）")


# --------------------------------------------------------------------------
#  休息点
# --------------------------------------------------------------------------


def rest_site(state: dict) -> Decision:
    options = _options(state)
    player = state.get("player") or {}
    hp, max_hp = player.get("hp") or 1, player.get("max_hp") or 1
    ids = {o.get("id"): o for o in options}
    # 与路线推演同一把尺子：回的血按缺血程度加权，和升级一张牌（8 分）比；
    # 下一步就是 Boss 时，这口血要直接扛 Boss，权重翻倍
    healed = min(0.3 * max_hp, max_hp - hp)
    heal_value = healed * route._loss_weight(hp, max_hp) * 0.6 * (2.0 if _boss_ahead(state) else 1.0)
    if "HEAL" in ids and heal_value > 8.0:
        return Decision("pick", ids["HEAL"]["i"], f"血 {hp}/{max_hp}，回血（{heal_value:.1f} 分 > 升级 8 分）")
    ctx = deck_context(state)
    upgradable = [c for c in ctx.deck if deckval.upgrade_gain(c) > -9]
    if "SMITH" in ids and upgradable:
        top = deckval.upgrade_order(upgradable, ctx)[0]
        return Decision("pick", ids["SMITH"]["i"], f"血量够，升级（首选 {db().name(top)}）", hint="upgrade")
    for oid in ("LIFT", "DIG", "HEAL"):
        if oid in ids:
            return Decision("pick", ids[oid]["i"], f"选 {oid}")
    if options:
        return Decision("pick", options[0]["i"], f"选 {options[0].get('id')}")
    return _proceed_or_wait(state, "休息点已处理")


def _boss_ahead(state: dict) -> bool:
    """下一步是不是 Boss（休息点在 Boss 前时回血门槛更高）。"""
    m = state.get("map") or {}
    coord = m.get("coord") or {}
    for r, c, t, nxt in m.get("nodes") or []:
        if (r, c) == (coord.get("row"), coord.get("col")):
            return any(t2 == "Boss" for r2, c2, t2, _ in m.get("nodes") or [] if [r2, c2] in nxt)
    return False


# --------------------------------------------------------------------------
#  商店
# --------------------------------------------------------------------------

REMOVAL_IDS = ("MerchantCardRemovalEntry", "CardRemoval")


def shop(state: dict, bought: set[str]) -> Decision:
    options = [o for o in _options(state) if o.get("available") is not False]
    ctx = deck_context(state)
    gold = (state.get("run") or {}).get("gold") or 0
    potions = state.get("potions") or []
    free_slot = any(p is None for p in potions)

    candidates: list[tuple[float, dict, str, str | None]] = []
    for o in options:
        oid, cost = str(o.get("id", "")), o.get("cost") or 0
        if cost > gold or f"{oid}@{o['i']}" in bought:
            continue
        if any(k in oid for k in REMOVAL_IDS):
            worst = deckval.removal_order(ctx.deck, ctx)[0] if ctx.deck else None
            badness = -deckval.value_in_deck(worst, ctx) if worst else 0
            if badness > 0.8:
                candidates.append((2.0 + badness, o, f"除卡（删 {db().name(worst)}）", "negative"))
        elif oid in db().cards:
            v = deckval.value_in_deck(oid, ctx)
            if v >= deckval.skip_threshold(ctx) + 0.5:
                candidates.append((v - cost / 150, o, f"买牌 {db().name(oid)}（{v:+.2f}，{cost} 金）", None))
        elif oid in db().relics:
            v = deckval.relic_value(oid)
            candidates.append((1.0 + v - cost / 250, o, f"买遗物 {db().name(oid)}（{v:+.2f}，{cost} 金）", None))
        elif oid in db().potions and free_slot:
            v = deckval.potion_value(oid)
            if v > 0 and cost <= 80:
                candidates.append((v - cost / 100, o, f"买药水 {db().name(oid)}", None))
    if candidates:
        score, o, why, hint = max(candidates, key=lambda t: t[0])
        if score > 0:
            bought.add(f"{o.get('id')}@{o['i']}")
            return Decision("pick", o["i"], why, hint=hint)
    return _proceed_or_wait(state, f"商店没有值得买的（{gold} 金）")


# --------------------------------------------------------------------------
#  宝箱 / 遗物多选一
# --------------------------------------------------------------------------


def treasure(state: dict) -> Decision:
    options = _options(state)
    chest = _find(options, "Chest")
    if chest:
        return Decision("pick", chest["i"], "开箱")
    relics = [o for o in options if o.get("id") in db().relics]
    if relics:
        o = max(relics, key=lambda o: deckval.relic_value(o["id"]))
        return Decision("pick", o["i"], f"拿遗物 {db().name(o['id'])}")
    return _proceed_or_wait(state, "宝箱已拿完")


def crystal_sphere(state: dict) -> Decision:
    """水晶球开格子小游戏：没有格子内容的信息，从网格中心往外开；占卜用完后按继续。"""
    cells = []
    for o in _options(state):
        try:
            x, y = map(int, str(o.get("id", "")).removeprefix("Cell:").split(","))
        except ValueError:
            continue
        cells.append((x, y, o["i"]))
    if cells:
        cx = sum(c[0] for c in cells) / len(cells)
        cy = sum(c[1] for c in cells) / len(cells)
        x, y, i = min(cells, key=lambda c: ((c[0] - cx) ** 2 + (c[1] - cy) ** 2, c[1], c[0]))
        return Decision("pick", i, f"水晶球：开格子 ({x},{y})")
    return _proceed_or_wait(state, "水晶球占卜已用完")


def relic_choice(state: dict) -> Decision:
    options = _options(state)
    relics = [o for o in options if o.get("id") in db().relics]
    if relics:
        scored = sorted(relics, key=lambda o: deckval.relic_value(o["id"]), reverse=True)
        line = "、".join(f"{db().name(o['id'])} {deckval.relic_value(o['id']):+.2f}" for o in scored)
        return Decision("pick", scored[0]["i"], f"遗物：{line}")
    skip = _find(options, "Alt:Skip")
    if skip:
        return Decision("pick", skip["i"], "没有认识的遗物，跳过")
    return _proceed_or_wait(state, "遗物选择已完成")


# --------------------------------------------------------------------------
#  事件
# --------------------------------------------------------------------------

# 事件选项的描述是渲染后的文本（带具体数值），按得失粗估。中英文都认。
_NUM = r"(\d+)"
EVENT_RULES: list[tuple[str, str]] = [
    (rf"失去{_NUM}点?最大生命|lose {_NUM} max hp", "max_hp_loss"),
    (rf"最大生命值?(?:上限)?(?:增加|提高|\+){_NUM}|(?:获得|增加){_NUM}点?最大生命|gain {_NUM} max hp", "max_hp_gain"),
    (rf"失去{_NUM}点?生命|受到{_NUM}点?伤害|lose {_NUM} hp|take {_NUM} damage", "hp_loss"),
    (rf"(?:回复|恢复|治疗){_NUM}|heal {_NUM}", "heal"),
    (rf"(?:获得|得到){_NUM}(?:枚)?金|gain {_NUM} gold", "gold_gain"),
    (rf"(?:失去|支付|花费){_NUM}(?:枚)?金|(?:lose|pay) {_NUM} gold", "gold_loss"),
    (r"移除|删除|remove", "remove"),
    (r"升级|upgrade", "upgrade"),
    (r"变化|transform", "transform"),
    (r"诅咒|curse", "curse"),
    (r"遗物|relic", "relic"),
    (r"药水|potion", "potion"),
    (r"战斗|fight|combat", "fight"),
]


def event_value(option: dict, state: dict) -> tuple[float, list[str]]:
    player = state.get("player") or {}
    hp, max_hp = player.get("hp") or 1, player.get("max_hp") or 1
    text = f"{option.get('title') or ''} {option.get('text') or ''}".lower()
    hp_w = 1.0 + 2.5 * (1 - hp / max_hp) ** 2
    ctx = deck_context(state)
    value, notes = 0.0, []

    def num(m: re.Match) -> int:
        return int(next(g for g in m.groups() if g))

    for pattern, kind in EVENT_RULES:
        m = re.search(pattern, text)
        if not m:
            continue
        notes.append(kind)
        if kind == "max_hp_loss":
            value -= num(m) * 1.6
        elif kind == "max_hp_gain":
            value += num(m) * 1.0
        elif kind == "hp_loss":
            n = num(m)
            value -= n * hp_w if n < hp else 999
        elif kind == "heal":
            value += min(num(m), max_hp - hp) * 0.6 * hp_w
        elif kind == "gold_gain":
            value += num(m) / 12
        elif kind == "gold_loss":
            value -= num(m) / 20
        elif kind == "remove":
            worst = deckval.removal_order(ctx.deck, ctx)[0] if ctx.deck else None
            value += 4 + max(0.0, -deckval.value_in_deck(worst, ctx) * 3) if worst else 0
        elif kind == "upgrade":
            value += 5
        elif kind == "transform":
            value += 2
        elif kind == "curse":
            value -= 10
        elif kind == "relic":
            value += 10 + (deckval.relic_value(option["relic"]) * 4 if option.get("relic") else 0)
        elif kind == "potion":
            value += 3
        elif kind == "fight":
            value -= 6 * hp_w
    if option.get("relic") and "relic" not in notes:
        value += 10 + deckval.relic_value(option["relic"]) * 4
        notes.append("relic")
    return value, notes


def event(state: dict) -> Decision:
    options = _options(state)
    if not options:
        return _proceed_or_wait(state, "事件已结束")
    scored = sorted(((event_value(o, state), o) for o in options), key=lambda t: t[0][0], reverse=True)
    (v, notes), o = scored[0]
    title = o.get("title") or o.get("id")
    line = "；".join(f"{x.get('title') or x.get('id')} {s:+.1f}" for (s, _), x in scored)
    return Decision("pick", o["i"], f"事件选「{title}」（{line}）")
