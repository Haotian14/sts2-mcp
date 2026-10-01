"""构筑：卡牌 / 遗物 / 药水的估值，以及选牌、跳过、除卡、升级的决策。

估值的单位是「标准差」（z 分数），各项可以直接相加：

    value = 社区基准 + 本局协同 + 牌组缺口 − 稀释与重复

社区基准来自约 190 万局真实对局（tools/fetch_metrics.py）：
- **Elo** 是「同时被提供时更常被选」的偏好强度 —— 衡量「三选一该拿哪张」最直接的量；
- **胜率** 有幸存者偏差（活得久的局才见得到稀有牌），只作小权重，并按样本量收缩。

协同来自两处：社区聚类出的主流构筑（archetypes 的定义牌），以及从卡面文本
抽出的机制标签（毒、小刀、奇巧、力量……）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable

from ..gamedata import base_id, db

# 分布参数取自 metrics.json（见 fetch_metrics.py）；数据刷新后差别不大，写死即可
ELO_MEAN, ELO_STD = 1511.0, 151.0
WIN_MEAN, WIN_STD = 43.1, 7.3
UPGRADE_DELTA_MEAN, UPGRADE_DELTA_STD = 17.3, 4.1
RELIC_WIN_MEAN, RELIC_WIN_STD = 54.0, 12.2
POTION_WIN_MEAN, POTION_WIN_STD = 36.8, 16.6

# 起始牌从不出现在奖励里，没有社区数据。打击/防御是公认的除卡首选。
BASIC_VALUE = {"Strike": -1.6, "Defend": -1.3}
OTHER_BASIC_VALUE = -0.4
CURSE_VALUE = -3.0
STATUS_VALUE = -2.0

# 机制标签：卡面英文文本里出现这些词，就算这条线上的牌。
# 同一条线上的牌互相加分（「引擎件 + 收益件」要凑齐才有用）。
TAGS = {
    "poison": r"\bPoison",
    "shiv": r"\bShiv",
    "sly": r"\bSly\b",
    "discard": r"\bDiscard",
    "strength": r"\bStrength\b",
    "dexterity": r"\bDexterity\b",
    "exhaust": r"\bExhaust",
    "vulnerable": r"\bVulnerable\b",
    "weak": r"\bWeak\b",
    "orb": r"\b(Channel|Evoke|Orb|Focus)\b",
    "osty": r"\b(Osty|Summon)\b",
    "doom": r"\bDoom\b",
    "stars": r"\bStars?\b",
    "forge": r"\bForge\b",
    "retain": r"\bRetain\b",
    "block_scaling": r"\b(Plated|Barricade|Thorns)\b",
}
_TAG_RE = {k: re.compile(v) for k, v in TAGS.items()}

# 来源件：真正「产出」这条线资源的牌 / 遗物。同标签但不匹配这里的，是收益件
# （幻影之刃、精准、爆发……），没有来源时是一张空过的牌。
# 只列来源可能缺席的线；起始牌组天然自带来源的机制（弃牌、召唤、星辉）不列。
_NUM = r"(?:\[blue\])?(?:\{[^}]*\}|\d+|X\S*)(?:\[/blue\])?"
PRODUCERS = {
    "shiv": rf"(?i)\badd {_NUM} [^.]*?\bShiv",
    "poison": rf"(?i)\bapply {_NUM} \[gold\]Poison",
    "doom": rf"(?i)\bapply (?:{_NUM}|that much) \[gold\]Doom|\bApply \[gold\]Doom\[/gold\] equal",
    "exhaust": r"(?<!you )\[gold\]Exhaust\[/gold\]|\bExhaust up to",
    "orb": r"\[gold\]Channel(?:\[/gold\]| Lightning)",
}
_PRODUCER_RE = {k: re.compile(v) for k, v in PRODUCERS.items()}
# 收益件缺来源的扣分：一个来源都没有 / 只有一个。越往后越难补上来源，每章加重
PAYOFF_NO_SOURCE, PAYOFF_ONE_SOURCE, PAYOFF_PER_ACT = -0.8, -0.3, 0.4


@dataclass
class DeckContext:
    character: str                      # Silent / Ironclad …（/state 的 player.character）
    deck: list[str]                     # 牌库，升级牌带 +
    act: int = 1
    floor: int = 0
    relics: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.deck)

    def ids(self) -> list[str]:
        return [base_id(c) for c in self.deck]


# --------------------------------------------------------------------------
#  单卡
# --------------------------------------------------------------------------


def tags_of(card_id: str) -> set[str]:
    text = db().card(card_id).get("text") or ""
    return {k for k, r in _TAG_RE.items() if r.search(text)}


def produces(card_id: str) -> set[str]:
    """这张牌是哪些线的来源件。自带「消耗」关键词的牌本身就是消耗的燃料。"""
    d = db().card(card_id)
    text = d.get("text") or ""
    out = {k for k, r in _PRODUCER_RE.items() if r.search(text)}
    if "Exhaust" in (d.get("keywords") or []):
        out.add("exhaust")
    return out


def payoffs_of(card_id: str) -> set[str]:
    """这张牌是哪些线的收益件：带标签、该线有来源之分、自己又不产出。"""
    return (tags_of(card_id) & PRODUCERS.keys()) - produces(card_id)


def sources_of(tag: str, ctx: DeckContext, exclude: str | None = None) -> int:
    """牌组（含起始牌）和遗物里，这条线有几个来源。"""
    n = sum(1 for c in ctx.ids() if c != exclude and tag in produces(c))
    r = _PRODUCER_RE[tag]
    n += sum(1 for rid in ctx.relics if r.search((db().relics.get(rid) or {}).get("text") or ""))
    return n


def is_basic(card_id: str) -> bool:
    return db().card(card_id).get("rarity") == "Basic"


def base_value(card_id: str, act: int = 1) -> float:
    """不看牌组、只看社区数据的基准估值。"""
    cid = base_id(card_id)
    d = db().card(cid)
    if d.get("type") == "Curse" or d.get("pool") == "Curse":
        return CURSE_VALUE
    if d.get("type") == "Status" or d.get("pool") == "Status":
        return STATUS_VALUE
    if d.get("rarity") == "Basic":
        for prefix, v in BASIC_VALUE.items():
            if cid.startswith(prefix):
                return v
        return OTHER_BASIC_VALUE

    m = db().metric("cards", cid)
    if m.elo is None and m.win is None:
        return 0.0
    value = 0.0
    if m.elo is not None:
        value += 0.75 * (m.elo - ELO_MEAN) / ELO_STD
    if m.win is not None:
        shrink = m.n / (m.n + 3000)
        value += 0.35 * shrink * (m.win - WIN_MEAN) / WIN_STD

    # 分章的选取率：有的牌前期抢手、后期没人要（反之亦然）
    acts = [p for p in m.pick_by_act if p]
    if len(acts) == 3 and 1 <= act <= 3 and m.pick_by_act[act - 1]:
        mean = sum(acts) / 3
        value += 0.5 * math.log(m.pick_by_act[act - 1] / mean)
    return value


def value_in_deck(card_id: str, ctx: DeckContext) -> float:
    """这张牌放进**这副牌**里值多少。"""
    cid = base_id(card_id)
    d = db().card(cid)
    value = base_value(cid, ctx.act)
    if d.get("type") in ("Curse", "Status") or d.get("rarity") == "Basic":
        return value

    ids = ctx.ids()
    others = [c for c in ids if c != cid]

    # 收益件缺前置：按最缺的那条线算（刀刃陷阱同时是小刀和消耗的收益件，不重复扣）
    payoff = payoffs_of(cid)
    worst = min((sources_of(t, ctx, exclude=cid) for t in payoff), default=99)

    # 社区构筑：牌组里已有同一构筑的定义牌 → 顺着走。
    # 收益件一个来源都没有时不算：精准 + 幻影之刃凑在一起仍然一把刀都没有。
    key = d.get("key")
    for build in db().archetypes.get(ctx.character.lower(), []) if worst > 0 else []:
        if key in build["cards"]:
            have = sum(1 for c in set(others) if db().card(c).get("key") in build["cards"])
            value += 0.35 * min(have, 2)

    # 机制标签协同（起始牌不算：静默的打击不构成「小刀流」）。
    # 收益件只跟来源件协同。
    mine = tags_of(cid)
    if mine:
        counts: dict[str, int] = {}
        for c in others:
            if is_basic(c):
                continue
            for t in tags_of(c) & mine:
                if t in payoff and t not in produces(c):
                    continue
                counts[t] = counts.get(t, 0) + 1
        value += 0.12 * sum(min(n, 4) for n in counts.values())

    if worst == 0:
        value += PAYOFF_NO_SOURCE - PAYOFF_PER_ACT * (ctx.act - 1)
    elif worst == 1:
        value += PAYOFF_ONE_SOURCE

    # 牌组缺口
    effects = d.get("effects") or []
    is_aoe = any(e.get("op") == "attack" and e.get("to") == "all" for e in effects)
    if is_aoe and not any(_is_aoe(c) for c in others) and ctx.act <= 2:
        value += 0.4
    if d.get("type") == "Attack" and ctx.act == 1:
        real_attacks = sum(1 for c in others if db().card(c).get("type") == "Attack" and not is_basic(c))
        if real_attacks < 3:
            value += 0.3

    # 重复与费用曲线
    copies = ids.count(cid)
    value -= 0.3 * copies
    cost = d.get("cost")
    if isinstance(cost, int) and cost >= 2:
        heavy = sum(1 for c in others if isinstance(db().card(c).get("cost"), int) and db().card(c)["cost"] >= 2)
        value -= 0.08 * max(0, heavy - 3)
    return value


def _is_aoe(card_id: str) -> bool:
    return any(e.get("op") == "attack" and e.get("to") == "all"
               for e in db().card(card_id).get("effects") or [])


# --------------------------------------------------------------------------
#  决策
# --------------------------------------------------------------------------


def skip_threshold(ctx: DeckContext) -> float:
    """低于这个分就跳过。牌越多，一张普通牌越稀释抽到关键牌的概率。"""
    return -0.35 + 0.035 * max(0, ctx.size - 18) + 0.1 * (ctx.act - 1)


def pick_card(options: Iterable[str], ctx: DeckContext) -> tuple[str | None, float, str]:
    """卡牌奖励三选一。返回 (选中的牌或 None 表示跳过, 分数, 理由)。"""
    scored = sorted(((value_in_deck(c, ctx), c) for c in options), reverse=True)
    if not scored:
        return None, 0.0, "没有可选的牌"
    best_v, best = scored[0]
    line = "、".join(f"{db().name(c)} {v:+.2f}" for v, c in scored)
    threshold = skip_threshold(ctx)
    if best_v < threshold:
        return None, best_v, f"跳过：{line}（门槛 {threshold:+.2f}）"
    return best, best_v, f"选 {db().name(best)}：{line}"


def removal_order(deck: Iterable[str], ctx: DeckContext) -> list[str]:
    """最该除掉的排在前面。"""
    return sorted(deck, key=lambda c: (value_in_deck(c, ctx), c.endswith("+")))


def upgrade_gain(card_id: str) -> float:
    """升级带来的提升（z 分数量级）。"""
    if card_id.endswith("+"):
        return -9.0
    d = db().card(card_id)
    up = d.get("upgrade") or {}
    if not up:
        return -9.0
    gain = 0.0
    if up.get("cost", 0) < 0:
        gain += 0.8
    base_vars = d.get("vars") or {}
    for k, delta in (up.get("vars") or {}).items():
        base = base_vars.get(k) or 0
        if base > 0:
            gain += min(1.0, delta / base) * 0.6
        elif delta > 0:
            gain += 0.4
    if set(up.get("remove_keywords") or []) & {"Exhaust", "Ethereal"}:
        gain += 0.5
    if set(up.get("add_keywords") or []) & {"Innate", "Retain"}:
        gain += 0.3
    m = db().metric("cards", card_id)
    if m.win is not None and m.win_up is not None:
        shrink = m.n / (m.n + 3000)
        gain += 0.3 * shrink * ((m.win_up - m.win) - UPGRADE_DELTA_MEAN) / UPGRADE_DELTA_STD
    if d.get("type") == "Power":
        gain += 0.2
    return gain


def upgrade_order(deck: Iterable[str], ctx: DeckContext) -> list[str]:
    """最该升级的排在前面：本身重要（常打）× 升级提升大。"""
    def score(c: str) -> float:
        g = upgrade_gain(c)
        if g <= -9:
            return -99.0
        return g + 0.4 * max(value_in_deck(c, ctx), -1.0)
    return sorted(deck, key=score, reverse=True)


def relic_value(relic_id: str) -> float:
    d = db().relics.get(relic_id) or {}
    m = db().metric("relics", relic_id)
    v = 0.0
    if m.win is not None:
        v += (m.win - RELIC_WIN_MEAN) / RELIC_WIN_STD
    v += {"Starter": -1.0, "Common": 0.0, "Uncommon": 0.1, "Rare": 0.2, "Shop": 0.1,
          "Boss": 0.3, "Ancient": 0.3, "Event": 0.0}.get(d.get("rarity", ""), 0.0)
    return v


def potion_value(potion_id: str) -> float:
    m = db().metric("potions", potion_id)
    if m.win is None:
        return 0.0
    return (m.win - POTION_WIN_MEAN) / POTION_WIN_STD
