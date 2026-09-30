"""战斗：一个回合之内的出牌规划。

做法是**束搜索**：从当前局面出发，枚举「打哪张牌、打谁、喝哪瓶药」的序列，
每一步在一个简化的战斗模型里结算，到回合末用同一个评估函数打分，取分最高的
序列的**第一步**执行。执行后局面会变（抽到新牌、数值重算），所以每一步都重新规划。

数值来源分两层：
- /state 里 hand[].values / damage_vs / hits —— 游戏自己算好的**此刻**的数，
  已含力量、虚弱、易伤、遗物、升级；
- data/cards.json 的 effects —— 这张牌**做什么**（打几段、打谁、给谁上什么），
  从 sts2.dll 的 OnPlay 抽出来的。模拟中途才出现的变化（先上易伤再打、
  先加力量再打、刀刃之舞生成的小刀）按规则推算。

评估函数（回合末）：

    分数 = −预计掉血 × 血量权重
           + 造成的有效伤害 × 伤害权重（按怪的威胁加权，溢出不算）
           + 击杀奖励（它今后每一回合的输出都没了）
           + 长期收益（中毒、易伤、虚弱、力量/敏捷与能力牌）
           + 抽牌收益 − 药水成本

规则里几条从实战里学到的要点，全都体现在评估里，而不是写成一条条 if：
斩杀优先于格挡（击杀奖励 + 意图归零）；敌人不攻击就不叠格挡（格挡只抵掉来袭）；
斩杀线要算敌人的格挡；毒在敌人行动前结算，毒死的怪不会出手。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..gamedata import db
from . import deck as deckval

BEAM_WIDTH = 48
MAX_DEPTH = 14

DMG_W = 0.55          # 1 点有效伤害 ≈ 0.55 点血
KILL_BASE = 4.0
ALL_DEAD_BONUS = 40.0
DEATH_PENALTY = 1000.0
DRAW_W = 1.4          # 抽 1 张（还有能量打它）
DEBUFF_EARLY = 0.03   # 减益越早挂越好的微小偏好，只用来打破平局
POTION_KEEP = {"monster": 6.0, "elite": 2.5, "boss": 0.0}

# 能力的长期价值：每层每回合约等于多少分。没列出的己方能力按能力牌的社区估值折算。
SELF_POWER_PER_TURN = {
    "StrengthPower": 1.2, "DexterityPower": 0.8, "ThornsPower": 0.5, "PlatingPower": 0.9,
    "RegenPower": 0.5, "FocusPower": 1.5, "AccuracyPower": 0.9,
}


# --------------------------------------------------------------------------
#  模型
# --------------------------------------------------------------------------


@dataclass
class Card:
    i: int | None              # 手牌下标；模拟中生成的牌为 None
    id: str
    cost: int | str | None     # "X" / None(打不出)
    type: str
    target: str
    values: dict[str, int]
    damage_vs: list[int] | None = None
    hits: int | None = None
    hits_vs: list[int] | None = None
    playable: bool = True

    @property
    def defn(self) -> dict:
        return db().card(self.id)


@dataclass
class Foe:
    i: int
    id: str
    hp: int
    block: int
    hittable: bool
    intent: int                 # 本回合总伤害
    intent_hits: int
    vulnerable: bool
    weak: bool
    poison: int                 # 已有的毒
    hp0: int = 0
    vuln_add: int = 0
    weak_add: int = 0
    poison_add: int = 0
    str_down: int = 0

    @property
    def alive(self) -> bool:
        return self.hp > 0


@dataclass
class Node:
    energy: int
    block: int
    hand: list[Card]
    foes: list[Foe]
    potions: list[str | None]
    str_gain: int = 0
    dex_gain: int = 0
    hp_cost: int = 0
    future: float = 0.0
    first: dict | None = None
    line: list[str] = field(default_factory=list)


@dataclass
class Ctx:
    """整回合不变的量。"""
    hp: int
    max_hp: int
    room: str                   # monster / elite / boss
    player_powers: dict[str, int]
    turns_left: float
    max_threat: int


# --------------------------------------------------------------------------
#  从 /state 建模
# --------------------------------------------------------------------------


def _powers(entity: dict) -> dict[str, int]:
    return {p.get("id", ""): p.get("amount") or 0 for p in entity.get("powers") or []}


def build(state: dict) -> tuple[Node, Ctx]:
    combat = state.get("combat") or {}
    player = state.get("player") or {}
    foes = []
    for e in state.get("enemies") or []:
        if not e.get("alive"):
            continue
        powers = _powers(e)
        intents = e.get("intents") or []
        total = sum(x["total"] for x in intents if isinstance(x.get("total"), int))
        hits = sum((x.get("repeats") or 1) for x in intents if isinstance(x.get("total"), int))
        foes.append(Foe(
            i=e["i"], id=e.get("id", "?"), hp=e.get("hp") or 0, block=e.get("block") or 0,
            hittable=e.get("hittable", True), intent=total, intent_hits=hits,
            vulnerable=powers.get("VulnerablePower", 0) > 0, weak=powers.get("WeakPower", 0) > 0,
            poison=powers.get("PoisonPower", 0), hp0=e.get("hp") or 0,
        ))
    hand = [Card(i=c["i"], id=c.get("id", "?"), cost=_cost(c), type=c.get("type") or "",
                 target=c.get("target") or "", values=c.get("values") or {},
                 damage_vs=c.get("damage_vs"), hits=c.get("hits"), hits_vs=c.get("hits_vs"),
                 playable=bool(c.get("playable")))
            for c in state.get("hand") or []]
    total_hp = sum(f.hp + f.block for f in foes)
    room = _room(state)
    ctx = Ctx(hp=player.get("hp") or 0, max_hp=player.get("max_hp") or 1, room=room,
              player_powers=_powers(player),
              turns_left=max(1.0, min(6.0, total_hp / 12.0)),
              max_threat=max([f.intent for f in foes] + [1]))
    node = Node(energy=combat.get("energy") or 0, block=player.get("block") or 0,
                hand=hand, foes=foes, potions=list(state.get("potions") or []))
    return node, ctx


def _cost(c: dict) -> int | str | None:
    if db().card(c.get("id", "")).get("cost") == "X":
        return "X"
    return c.get("cost")


def _room(state: dict) -> str:
    enc = (state.get("combat") or {}).get("encounter") or ""
    room = (state.get("run") or {}).get("room") or ""
    for key in ("boss", "elite"):
        if key in enc.lower() or key in room.lower():
            return key
    return db().encounter(enc).get("room") or "monster"


# --------------------------------------------------------------------------
#  结算
# --------------------------------------------------------------------------


def _var(card: Card, name: Any, x: int) -> int:
    if name == "X":
        return x
    if isinstance(name, int):
        return name
    if isinstance(name, str):
        if name in card.values:
            return card.values[name]
        v = (card.defn.get("vars") or {}).get(name)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _per_hit(card: Card, foe: Foe, node: Node, ctx: Ctx, var: Any, x: int) -> int:
    if card.i is not None and card.damage_vs and 0 <= foe.i < len(card.damage_vs):
        dmg = card.damage_vs[foe.i]
        already_vuln = foe.vulnerable
    else:
        dmg = _var(card, var, x)
        if card.i is None:          # 模拟中生成的牌：自己加上力量、精准与易伤
            dmg += ctx.player_powers.get("StrengthPower", 0)
            if card.id == "Shiv":
                dmg += ctx.player_powers.get("AccuracyPower", 0)
            if ctx.player_powers.get("WeakPower", 0) > 0:
                dmg = int(dmg * 0.75)
            already_vuln = False
        else:
            already_vuln = foe.vulnerable
    dmg += node.str_gain
    if foe.vuln_add > 0 and not already_vuln:
        dmg = int(dmg * 1.5)
    # 跟踪（TrackingPower）：对虚弱的敌人伤害 ×层数。已经虚弱的，实时数值里已含；
    # 本回合模拟中才挂上的虚弱要自己乘 —— 这就是「先打中和」的收益来源。
    tracking = ctx.player_powers.get("TrackingPower", 0)
    if tracking > 1 and foe.weak_add > 0 and not foe.weak:
        dmg = int(dmg * tracking)
    return max(0, dmg)


def _hit(foe: Foe, dmg: float) -> None:
    absorbed = min(foe.block, dmg)
    foe.block -= int(absorbed)
    foe.hp -= int(round(dmg - absorbed))


def _hits(card: Card, e: dict, foe: Foe | None, x: int) -> int:
    if e.get("hits") == "X":
        return x
    if card.i is not None:
        if foe is not None and card.hits_vs and 0 <= foe.i < len(card.hits_vs):
            return card.hits_vs[foe.i]
        if card.hits is not None:
            return card.hits
    return _var(card, e.get("hits", 1), x) or 1


def _clone(node: Node) -> Node:
    return replace(node, hand=list(node.hand), foes=[replace(f) for f in node.foes],
                   potions=list(node.potions), line=list(node.line))


def play(node: Node, ctx: Ctx, card: Card, target: Foe | None) -> Node | None:
    if not card.playable or card.cost is None:
        return None
    x = node.energy if card.cost == "X" else 0
    cost = x if card.cost == "X" else int(card.cost)
    if cost > node.energy:
        return None

    n = _clone(node)
    n.energy -= cost
    n.hand = [c for c in n.hand if c is not card]
    tgt = next((f for f in n.foes if target is not None and f.i == target.i), None)
    defn = card.defn
    effects = defn.get("effects") or []
    _apply_effects(n, ctx, card, effects, tgt, x)

    if not defn or not effects:
        # 不认识的牌：只按社区估值给一点分，保证能量富余时会打出它
        n.future += max(0.3, 1.0 + deckval.base_value(card.id))
    elif defn.get("complex"):
        n.future += 0.5
    n.line.append(_label(card, tgt))
    if n.first is None:
        n.first = {"card": card.i, "target": tgt.i if tgt and _needs_target(card) else None}
    return n


def _apply_effects(n: Node, ctx: Ctx, card: Card, effects: list[dict], tgt: Foe | None, x: int) -> None:
    for e in effects:
        op = e.get("op")
        if op == "attack":
            alive = [f for f in n.foes if f.alive and f.hittable]
            if e.get("to") == "all":
                targets = [(f, 1.0) for f in alive]
            elif e.get("to") == "random":
                targets = [(f, 1.0 / len(alive)) for f in alive] if alive else []
            else:
                targets = [(tgt, 1.0)] if tgt and tgt.alive else []
            for foe, share in targets:
                per = _per_hit(card, foe, n, ctx, e.get("var"), x)
                for _ in range(_hits(card, e, foe, x)):
                    if foe.alive:
                        _hit(foe, per * share)
        elif op == "block":
            amount = _var(card, e.get("var"), x)
            if card.i is None:
                amount += ctx.player_powers.get("DexterityPower", 0)
            n.block += amount + n.dex_gain
        elif op == "apply":
            amount = _var(card, e.get("var"), x) or 1
            power = e.get("power", "")
            if e.get("to") == "self":
                _self_power(n, ctx, power, amount)
            else:
                targets = [f for f in n.foes if f.alive] if e.get("to") == "all" else ([tgt] if tgt else [])
                for foe in targets:
                    _foe_power(foe, power, amount)
                # 同分时先挂减益：没有坏处，而且挂上后意图与数值当场更新，后续规划更准
                n.future += DEBUFF_EARLY * max(0, MAX_DEPTH - len(n.line))
        elif op == "draw":
            # 抽到的牌要有能量打才值钱：剩的能量越多，这次抽牌越值（也就越该先打）
            k = _var(card, e.get("var"), x)
            n.future += k * (0.35 + DRAW_W * min(n.energy, 3) / 3)
        elif op == "energy":
            n.energy += _var(card, e.get("var"), x)
        elif op == "make" and e.get("pile") == "hand":
            made = db().card(e.get("card", ""))
            count = _var(card, e.get("count"), x) if not isinstance(e.get("count"), int) else e["count"]
            for _ in range(max(0, min(count, 10 - len(n.hand)))):
                n.hand.append(Card(i=None, id=e["card"], cost=made.get("cost"), type=made.get("type", ""),
                                   target=made.get("target", ""), values={}))
        elif op == "choose" and e.get("kind") in ("discard", "exhaust"):
            if n.hand:
                worst = min(n.hand, key=lambda c: _keep_value(c))
                n.hand = [c for c in n.hand if c is not worst]
                n.future += 0.4 if "Sly" in (worst.defn.get("keywords") or []) else 0.0
        elif op == "lose_hp":
            n.hp_cost += _var(card, e.get("var"), x)
        elif op == "heal":
            n.future += 0.5 * _var(card, e.get("var"), x)


def _self_power(n: Node, ctx: Ctx, power: str, amount: int) -> None:
    if power == "StrengthPower":
        n.str_gain += amount
    elif power == "DexterityPower":
        n.dex_gain += amount
    per_turn = SELF_POWER_PER_TURN.get(power)
    if per_turn is None:
        per_turn = 0.6
    n.future += per_turn * amount * max(0.0, ctx.turns_left - 1)


def _foe_power(foe: Foe, power: str, amount: int) -> None:
    if power == "VulnerablePower":
        foe.vuln_add += amount
    elif power == "WeakPower":
        foe.weak_add += amount
    elif power == "PoisonPower":
        foe.poison_add += amount
    elif power == "StrengthPower" and amount < 0:
        foe.str_down += -amount


def _keep_value(c: Card) -> float:
    """弃牌/消耗时的取舍：越低越先丢。虚无的诅咒留在手里（回合末会自己消耗掉）。"""
    kws = c.defn.get("keywords") or []
    if "Sly" in kws:
        return -10.0         # 奇巧：弃掉等于免费打出
    if c.type in ("Curse", "Status"):
        return 5.0 if "Ethereal" in kws else -5.0
    return deckval.base_value(c.id)


def _needs_target(card: Card) -> bool:
    return card.target in ("AnyEnemy", "AnyAlly")


def _label(card: Card, tgt: Foe | None) -> str:
    name = db().name(card.id)
    return f"{name}→{db().name(tgt.id)}" if tgt and _needs_target(card) else name


# --------------------------------------------------------------------------
#  药水
# --------------------------------------------------------------------------


def use_potion(node: Node, ctx: Ctx, slot: int, target: Foe | None) -> Node | None:
    pid = node.potions[slot]
    d = db().potions.get(pid or "") or {}
    if not pid or d.get("usage") not in (None, "CombatOnly", "AnyTime") or d.get("complex"):
        return None
    effects = d.get("effects") or []
    if not effects:
        return None
    n = _clone(node)
    n.potions[slot] = None
    fake = Card(i=None, id=pid, cost=0, type="Potion", target=d.get("targettype", ""),
                values={k: int(v) for k, v in (d.get("vars") or {}).items()})
    tgt = next((f for f in n.foes if target is not None and f.i == target.i), None)
    # 药水的 GainBlock 目标写作 target（AnyPlayer），在抽取结果里是 "enemy"/"other"：按自身处理
    fixed = [dict(e, to="self") if e.get("op") == "apply" and d.get("targettype") in ("AnyPlayer", "Self")
             else e for e in effects]
    _apply_effects(n, ctx, fake, fixed, tgt, 0)
    n.future -= POTION_KEEP.get(ctx.room, 4.0) + max(0.0, deckval.potion_value(pid))
    n.line.append(f"药水 {db().name(pid)}")
    if n.first is None:
        n.first = {"potion": slot, "target": tgt.i if tgt and d.get("targettype") == "AnyEnemy" else None}
    return n


# --------------------------------------------------------------------------
#  评估
# --------------------------------------------------------------------------


def evaluate(n: Node, ctx: Ctx) -> float:
    hp_w = 1.0 + 1.5 * (1 - ctx.hp / max(1, ctx.max_hp))
    incoming = 0
    score = n.future

    for f in n.foes:
        poison = f.poison + f.poison_add
        dies_to_poison = f.alive and poison >= f.hp
        if f.alive and not dies_to_poison:
            intent = f.intent
            if f.weak_add > 0 and not f.weak and intent:
                intent = int(intent * 0.75)
            if f.str_down and f.intent_hits:
                intent = max(0, intent - f.str_down * f.intent_hits)
            incoming += intent

        threat = 0.6 + 0.8 * (f.intent / ctx.max_threat)
        dealt = f.hp0 - max(0, f.hp)
        score += dealt * DMG_W * threat
        if not f.alive or dies_to_poison:
            score += KILL_BASE + 1.5 * max(f.intent, 5)
        else:
            if f.poison_add:
                ticks = sum(max(0, poison - k) for k in range(int(min(poison, ctx.turns_left + 1))))
                already = sum(max(0, f.poison - k) for k in range(int(min(f.poison, ctx.turns_left + 1))))
                score += min(ticks - already, f.hp) * DMG_W * 0.85
            if f.vuln_add and not f.vulnerable:
                score += min(f.vuln_add, 3) * 2.5 * DMG_W
            if f.weak_add:
                score += max(0, min(f.weak_add, 3) - 1) * 0.25 * max(f.intent, 6) * 0.5
    if all(not f.alive or f.poison + f.poison_add >= f.hp for f in n.foes):
        score += ALL_DEAD_BONUS
        incoming = 0

    loss = max(0, incoming - n.block) + n.hp_cost
    if loss >= ctx.hp:
        score -= DEATH_PENALTY + loss
    score -= loss * hp_w
    return score


# --------------------------------------------------------------------------
#  搜索
# --------------------------------------------------------------------------


def _children(node: Node, ctx: Ctx) -> list[Node]:
    out = []
    targets = [f for f in node.foes if f.alive and f.hittable]
    seen_cards: set[tuple] = set()
    for card in node.hand:
        sig = (card.id, card.cost, tuple(sorted(card.values.items())), card.i is None)
        if sig in seen_cards:
            continue            # 两张一模一样的牌只展开一张
        seen_cards.add(sig)
        if _needs_target(card):
            for t in targets:
                child = play(node, ctx, card, t)
                if child:
                    out.append(child)
        else:
            child = play(node, ctx, card, None)
            if child:
                out.append(child)
    for slot, pid in enumerate(node.potions):
        if not pid:
            continue
        needs = (db().potions.get(pid) or {}).get("targettype") == "AnyEnemy"
        for t in (targets if needs else [None]):
            child = use_potion(node, ctx, slot, t)
            if child:
                out.append(child)
    return out


def _key(n: Node) -> tuple:
    return (n.energy, n.block, tuple(sorted(c.id for c in n.hand)),
            tuple((f.hp, f.block, f.vuln_add, f.weak_add, f.poison_add) for f in n.foes),
            tuple(p or "" for p in n.potions), n.str_gain, n.dex_gain)


@dataclass
class Plan:
    move: dict | None           # {"card": i, "target": j} / {"potion": s, "target": j} / None=结束回合
    score: float
    line: list[str]

    def why(self) -> str:
        seq = " → ".join(self.line) if self.line else "（不出牌）"
        return f"规划 {seq}，评分 {self.score:+.1f}"


def plan_turn(state: dict) -> Plan:
    root, ctx = build(state)
    best = Plan(None, evaluate(root, ctx), [])
    frontier = [root]
    for _ in range(MAX_DEPTH):
        scored: dict[tuple, tuple[float, Node]] = {}
        for node in frontier:
            for child in _children(node, ctx):
                s = evaluate(child, ctx)
                k = _key(child)
                if k not in scored or s > scored[k][0]:
                    scored[k] = (s, child)
                if s > best.score + 1e-9:
                    best = Plan(child.first, s, child.line)
        if not scored:
            break
        ranked = sorted(scored.values(), key=lambda t: t[0], reverse=True)
        frontier = [n for _, n in ranked[:BEAM_WIDTH]]
    return best
