"""战斗启发式与决策分层（spec.md 的 6.1 + 6.2）。

【为什么在 Python 侧而不在桥接层里】
spec.md §4 定的位置：自动驾驶循环属于 MCP server。理由很实在 ——
改一版启发式不用重编译、不用重启游戏，而桥接层每改一次都得重启一遍。
且这里需要的全部信息，`/state` 已经给全了。

【本模块的第一原则：分层比启发式本身更重要】
2026-08-01 有一版「朴素」原型打死了一整局。事后复盘，根本错误不在出牌逻辑，
而在**它不该接管那个局面**（HP 37/70 面对四只怪，属于 spec §4 的第 3 档）。
所以这里的结构是：**先判安全线，过不了就一张牌都不打，原样交还模型**。
`_handoff_reason` 写在最前面，就是这个意思。

【出牌逻辑照 spec §6.2 的三条硬性要求写】
1. 格挡是硬约束 —— 先算出所需格挡量并满足它，剩余能量才用于输出；
2. 可打出 ≠ 值得打 —— 显式白名单，Status / Curse 一律排除；
3. 结束回合前必须确认无更优出牌 —— 还有认不出的牌就交还，不闷头结束回合。
"""

from __future__ import annotations

from typing import Any, Callable

# --------------------------------------------------------------------------
#  安全线（6.1 分层）
#
#  阈值直接取自 strategy.md §3 的「必须交还的信号」表。改这里等于改策略，
#  两边必须同步。
# --------------------------------------------------------------------------

LOW_HP_RATIO = 0.4          # hp < max_hp * 0.4 → 交还
DAMAGE_RATIO = 0.25         # 预计净掉血 > hp * 1/4 → 交还

# 伤害变量有两种名字：普通攻击牌是 "Damage"，条件伤害走 "CalculatedDamage"。
# 实测「完美打击」只有后者（`{CalculationBase:6, ExtraDamage:2,
# CalculatedDamage:22}`），只认前者会把它当成不认识的牌而白白交还。
DAMAGE_KEYS = ("Damage", "CalculatedDamage")

# 本启发式只认识伤害与格挡。牌上带别的（抽牌、上能力、消耗…）一律不碰 ——
# 这正是「可打出 ≠ 值得打」那条要求：不认识就不打，而不是打了再说。
KNOWN_EFFECTS = DAMAGE_KEYS + ("Block",)

# 塞进来的废牌与诅咒。v1 原型在 12 血面对 22 点伤害时打了两张 Slimed，然后死了。
JUNK_TYPES = ("Status", "Curse")


def _alive(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in state.get("enemies") or [] if e.get("alive")]


def _incoming(enemies: list[dict[str, Any]]) -> int:
    """本回合将要挨的总伤害。

    只统计带 `total` 的意图 —— Buff / Sleep / Debuff 这类没有 total，
    它们本回合不造成伤害（strategy.md §1.3）。
    """
    total = 0
    for e in enemies:
        for intent in e.get("intents") or []:
            if isinstance(intent.get("total"), int):
                total += intent["total"]
    return total


def _intent_total(enemy: dict[str, Any]) -> int:
    return sum(i["total"] for i in enemy.get("intents") or [] if isinstance(i.get("total"), int))


def handoff_reason(state: dict[str, Any]) -> str | None:
    """该不该把方向盘交回给模型。返回原因字符串，None 表示可以本地接管。

    每个回合开始时都要重判 —— 局面是会变的，第 1 回合安全不代表第 4 回合安全。
    那次打死一整局的原型，正是死在「一旦接管就一路打到底」。
    """
    if not state.get("in_run"):
        return "不在局中"
    if (state.get("run") or {}).get("game_over"):
        return "本局已结束"
    if not state.get("in_combat"):
        return "不在战斗中"

    # 待答的选择一律交还。弃牌选择照 strategy.md §1.7 本可以本地做，但
    # `choice` 不告诉我们这是弃牌还是检索 —— 两者的最优选法**正好相反**
    # （弃牌丢最没用的，检索取最有用的）。分不清就不做。
    if state.get("awaiting_choice"):
        return "游戏正等一次选择（弃牌/检索/除卡），语义分不清，交还"

    player = state.get("player") or {}
    hp, max_hp = player.get("hp"), player.get("max_hp")
    if not isinstance(hp, int) or not isinstance(max_hp, int) or max_hp <= 0:
        return "读不到血量"

    if hp < max_hp * LOW_HP_RATIO:
        return f"血量过低（{hp}/{max_hp} < {LOW_HP_RATIO:.0%}）"

    enemies = _alive(state)
    if not enemies:
        return None      # 怪都死了，等结算即可，不必交还

    incoming = _incoming(enemies)
    block = player.get("block") or 0

    # 致死风险优先于一切 —— 哪怕比例判据还没触发
    if incoming >= hp:
        return f"来袭 {incoming} ≥ 当前血量 {hp}，有致死风险"

    # 「预计」掉血必须把**本回合还能叠出来的格挡**算进去。
    #
    # 判断发生在回合开始，此刻 player.block 必然是 0 —— 只看它等于假定我们
    # 一点格挡都不打，于是每个「来袭超过血量 1/4」的普通回合都会交还，
    # 分层就退化成了「几乎全交给模型」。2026-08-01 实测：一场 39 血小怪的
    # 常规战斗，第 3 回合就因此停手，而当时手里明明有 16 点格挡的血墙。
    #
    # 注意这**不会**放松 v1 那个死亡局面：HP 37/70 面对 26 点来袭、手上多是
    # 废牌时，能叠出来的格挡远补不上缺口，照样交还。
    capacity = _max_block(state.get("hand") or [],
                          (state.get("combat") or {}).get("energy") or 0)
    net = incoming - block - capacity
    threshold = hp * DAMAGE_RATIO
    if net > threshold:
        return (f"预计净掉血 {net}（来袭 {incoming} − 现有格挡 {block} − "
                f"本回合还能叠 {capacity}）超过血量的 1/4（{threshold:.0f}）")

    return None


# --------------------------------------------------------------------------
#  手牌评估
# --------------------------------------------------------------------------


def _usable(card: dict[str, Any]) -> bool:
    """值不值得打 —— 与「能不能打」（`playable`）是两回事。"""
    if not card.get("playable"):
        return False
    if card.get("type") in JUNK_TYPES:
        return False
    values = card.get("values") or {}
    return any(k in values for k in KNOWN_EFFECTS)


def _unknown_affordable(state: dict[str, Any]) -> list[dict[str, Any]]:
    """打得起、但本启发式不认识的牌。

    有这种牌就不能闷头结束回合（要求 3）—— 它可能正是这一回合的最优解。
    """
    return [
        c for c in state.get("hand") or []
        if c.get("playable") and c.get("type") not in JUNK_TYPES
        and not any(k in (c.get("values") or {}) for k in KNOWN_EFFECTS)
    ]


def _damage(card: dict[str, Any], enemy_i: int) -> int:
    """这张牌打在第 enemy_i 只怪身上的**实际**伤害。

    `damage_vs` 含目标侧修正（易伤），只在与 `values.Damage` 不同时才出现 ——
    没有它就说明对谁都一样。绝不能用 get_glossary 的卡面文本，那是裸值
    （strategy.md §4，为此死过一局）。
    """
    vs = card.get("damage_vs")
    if isinstance(vs, list) and 0 <= enemy_i < len(vs) and isinstance(vs[enemy_i], int):
        return vs[enemy_i]
    return _base_damage(card)


def _base_damage(card: dict[str, Any]) -> int:
    values = card.get("values") or {}
    for key in DAMAGE_KEYS:
        if isinstance(values.get(key), int):
            return values[key]
    return 0


def _block(card: dict[str, Any]) -> int:
    return (card.get("values") or {}).get("Block") or 0


def _cost(card: dict[str, Any]) -> int:
    c = card.get("cost")
    return c if isinstance(c, int) else 0


def _needs_target(card: dict[str, Any]) -> bool:
    return card.get("target") in ("AnyEnemy", "AnyAlly")


def _attacks(hand: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in hand if _usable(c) and _base_damage(c) > 0]


def _blocks(hand: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in hand if _usable(c) and _block(c) > 0]


# --------------------------------------------------------------------------
#  单步决策
# --------------------------------------------------------------------------


def _killable(hand: list[dict[str, Any]], energy: int, enemy: dict[str, Any],
              enemy_i: int) -> list[dict[str, Any]] | None:
    """能否在本回合内凑出足够伤害打死这只怪？返回要打的牌，凑不出返回 None。

    strategy.md §1.1：杀掉一只怪是把它**今后每一回合**的输出都归零，
    远比挡住这一回合划算。§1.2：凑数字，打在死怪身上的伤害等于零。

    贪心（先打伤害高的）不保证最省能量，但绝不会高估 —— 凑得出就是真凑得出。
    """
    hp = enemy.get("hp")
    if not isinstance(hp, int) or hp <= 0:
        return None
    if not enemy.get("hittable"):
        return None

    picked: list[dict[str, Any]] = []
    dealt = spent = 0
    for card in sorted(_attacks(hand), key=lambda c: _damage(c, enemy_i), reverse=True):
        if spent + _cost(card) > energy:
            continue
        picked.append(card)
        dealt += _damage(card, enemy_i)
        spent += _cost(card)
        if dealt >= hp:
            return picked
    return None


def _pick_block(hand: list[dict[str, Any]], energy: int) -> dict[str, Any] | None:
    """挡得最多的那张（能量够的前提下）。"""
    affordable = [c for c in _blocks(hand) if _cost(c) <= energy]
    return max(affordable, key=_block, default=None)


def _max_block(hand: list[dict[str, Any]], energy: int,
               exclude: list[dict[str, Any]] | None = None) -> int:
    """这些能量最多能叠出多少格挡。贪心取「挡得多的」，不保证最优但不会高估。"""
    skip = {id(c) for c in (exclude or [])}
    total = 0
    for c in sorted(_blocks(hand), key=_block, reverse=True):
        if id(c) in skip or _cost(c) > energy:
            continue
        total += _block(c)
        energy -= _cost(c)
    return total


def decide(state: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """本回合的下一步动作。返回 (动作, 理由)；动作为 None 表示该结束回合了。

    动作形如 {"card": i} 或 {"card": i, "target": j}。

    **每打出一张牌都要重新调用本函数**：手牌下标会重排，`values` 也会变
    （力量、虚弱、易伤都是实时的），沿用上一次的判断必然出错。
    """
    hand = state.get("hand") or []
    combat = state.get("combat") or {}
    player = state.get("player") or {}
    energy = combat.get("energy") or 0
    enemies = _alive(state)

    def act(card: dict[str, Any], enemy_i: int | None, why: str):
        move: dict[str, Any] = {"card": card["i"]}
        if _needs_target(card) and enemy_i is not None:
            move["target"] = enemies[enemy_i]["i"]
        return move, why

    if energy <= 0 or not hand:
        return None, "无能量或无手牌"
    if not enemies:
        return None, "场上已无存活敌人"

    # 敌人按「本回合打多少」排序：先处理威胁最大的那只
    order = sorted(range(len(enemies)), key=lambda i: _intent_total(enemies[i]), reverse=True)

    incoming = _incoming(enemies)
    block_now = player.get("block") or 0
    thorns = any(
        p.get("id", "").startswith("Thorns")
        for e in enemies for p in (e.get("powers") or [])
    )

    # ── 荆棘：顺序决定挨不挨打（strategy.md §1.4）────────────────────────
    # 反弹伤害会被格挡吃掉，所以有荆棘时必须**先叠格挡再攻击**。
    if thorns and incoming > block_now:
        card = _pick_block(hand, energy)
        if card:
            return act(card, None, f"场上有荆棘，先叠格挡再攻击（来袭 {incoming}，现有格挡 {block_now}）")

    # ── 斩杀（strategy.md §1.1）────────────────────────────────────────
    # 杀掉一只怪，它这回合的输出当场消失，而且**今后每一回合**都消失。
    #
    # 判据不是「杀完还挡不挡得满」——那道保险太严，会把 §1.1 那个实测局面
    # （怪 1 只剩 16 血却要打 16 点）判成堆格挡，而那一局的结论恰恰是斩杀对。
    # 正确的比法是**两条线各挨多少**：
    #
    #   斩杀线   incoming − 它的意图 − 剩余能量能叠出的格挡
    #   格挡线   incoming − 全部能量能叠出的格挡
    #
    # 谁挨得少走谁。这样既拿到了斩杀的收益，又不会像 v1 那样让斩杀把格挡挤掉
    # —— 真的挤掉时，格挡线会更划算，自然就不斩杀了。
    block_line = incoming - block_now - _max_block(hand, energy)

    for enemy_i in order:
        enemy = enemies[enemy_i]
        plan = _killable(hand, energy, enemy, enemy_i)
        if not plan:
            continue

        left = energy - sum(_cost(c) for c in plan)
        kill_line = (incoming - _intent_total(enemy)) - block_now - _max_block(hand, left, plan)
        if kill_line > block_line:
            continue     # 杀了反而挨得更多，放弃，走下面的格挡分支

        best = max(plan, key=lambda c: _damage(c, enemy_i))
        return act(best, enemy_i,
                   f"斩杀 {enemy.get('id')}（{enemy.get('hp')} 血，本回合打 "
                   f"{_intent_total(enemy)}）—— 斩杀线净挨 {max(0, kill_line)}，"
                   f"堆格挡净挨 {max(0, block_line)}")

    # ── 格挡：硬约束（spec §6.2 要求 1）──────────────────────────────
    # 「优先格挡」不能只是排序偏好，必须先满足所需量，剩余能量才用于输出。
    # 敌人不攻击的回合一点都不叠（§1.3）—— incoming 为 0 时这里自然跳过。
    need = incoming - block_now
    blocked_out = False        # 该挡但手上/能量上挡不了 —— 理由里必须说实话
    if need > 0:
        card = _pick_block(hand, energy)
        if card:
            return act(card, None, f"补格挡：来袭 {incoming}，现有 {block_now}，还差 {need}")
        blocked_out = True

    # ── 输出：剩余能量全砸给威胁最大的那只 ──────────────────────────────
    for enemy_i in order:
        affordable = [c for c in _attacks(hand) if _cost(c) <= energy]
        if not affordable:
            break
        best = max(affordable, key=lambda c: _damage(c, enemy_i))
        # 决策日志里写假理由比不写更糟：走到这里可能是「挡够了」，
        # 也可能是「该挡但没有防御牌」，两者必须分得清。
        why = (f"还差 {need} 点格挡但手上没有打得起的防御牌，转为输出"
               if blocked_out else "格挡已满足，剩余能量输出")
        return act(best, enemy_i,
                   f"{why} → {enemies[enemy_i].get('id')}（{_damage(best, enemy_i)} 点）")

    # 还有打得起的格挡牌就继续叠 —— 总比把能量浪费掉强
    card = _pick_block(hand, energy)
    if card:
        return act(card, None, "无攻击牌可打，剩余能量转为格挡")

    return None, "没有值得打出的牌了"


# --------------------------------------------------------------------------
#  回合循环
# --------------------------------------------------------------------------


def play_combat(
    state: dict[str, Any],
    play_card: Callable[[int, int | None], dict[str, Any]],
    end_turn: Callable[[], dict[str, Any]],
    max_turns: int = 20,
    on_step: Callable[[dict[str, Any], str, str], None] | None = None,
) -> dict[str, Any]:
    """自动打，直到战斗结束、触及安全线、或打满 max_turns 回合。

    `play_card` / `end_turn` 由调用方注入 —— 本模块因此不依赖 httpx，
    可以脱离游戏单测。

    `on_step(出手前的局面, 动作, 理由)` 是决策日志的挂钩点（6.5）。放在这里
    而不是让调用方事后遍历 `log`，是因为**出手前的局面只有这里有** ——
    事后拿到的 `log` 早已不知道当时是几点血、场上有几只怪。
    """
    log: list[dict[str, Any]] = []
    turns = 0

    def step(before: dict[str, Any], label: str, why: str) -> None:
        log.append({"turn": (before.get("combat") or {}).get("turn"), "play": label, "why": why})
        if on_step:
            try:
                on_step(before, label, why)
            except Exception:      # noqa: BLE001 —— 记日志失败绝不能打断战斗
                pass

    def snapshot(result: dict[str, Any]) -> dict[str, Any]:
        # 动作工具的返回值里带着执行后的新状态，不必再读一次
        return result.get("state") or {}

    while turns < max_turns:
        # 安全线每回合重判 —— 「一旦接管就一路打到底」正是打死一整局的那个 bug
        reason = handoff_reason(state)
        if reason:
            if not state.get("in_combat"):
                return _done(state, log, turns, "combat_ended", reason)
            return _done(state, log, turns, "handoff", reason)

        # 打完这一回合能打的牌
        while True:
            move, why = decide(state)
            if move is None:
                break
            before, label = state, _label(state, move)
            result = play_card(move["card"], move.get("target"))
            if not result.get("ok"):
                # 桥接层说这步不能走 —— 启发式和游戏的判断出现分歧，
                # 不猜、不重试，直接交还。
                return _done(snapshot(result) or state, log, turns, "handoff",
                             f"动作被拒绝（{result.get('error')}：{result.get('reason')}），"
                             f"启发式与游戏判断不一致，交还")
            step(before, label, why)
            state = snapshot(result) or state

            if not state.get("in_combat"):
                return _done(state, log, turns + 1, "combat_ended", "战斗结束")
            if state.get("awaiting_choice"):
                return _done(state, log, turns, "handoff",
                             "打出的牌引出一次选择（弃牌/检索），语义分不清，交还")

        # 要求 3：结束回合前确认没有更优出牌
        unknown = _unknown_affordable(state)
        if unknown:
            names = "、".join(c.get("id", "?") for c in unknown)
            return _done(state, log, turns, "handoff",
                         f"还有打得起、但本启发式不认识的牌（{names}）—— "
                         f"不闷头结束回合，交给你判断")

        before = state
        result = end_turn()
        if not result.get("ok"):
            return _done(state, log, turns, "handoff",
                         f"结束回合被拒绝（{result.get('error')}）")
        step(before, "end_turn", "本回合已无值得打出的牌")
        state = snapshot(result) or state
        turns += 1

        if not state.get("in_combat"):
            return _done(state, log, turns, "combat_ended", "战斗结束")

    return _done(state, log, turns, "max_turns", f"已打满 {max_turns} 回合，交还")


def _label(state: dict[str, Any], move: dict[str, Any]) -> str:
    hand = state.get("hand") or []
    card = next((c for c in hand if c.get("i") == move["card"]), None)
    name = (card or {}).get("id", f"#{move['card']}")
    if "target" in move:
        enemies = state.get("enemies") or []
        enemy = next((e for e in enemies if e.get("i") == move["target"]), None)
        return f"{name} → {(enemy or {}).get('id', move['target'])}"
    return name


def _done(state: dict[str, Any], log: list[dict[str, Any]], turns: int,
          stopped: str, reason: str) -> dict[str, Any]:
    return {
        "ok": True,
        "stopped": stopped,
        "reason": reason,
        "turns": turns,
        "log": log,
        "state": state,
    }
