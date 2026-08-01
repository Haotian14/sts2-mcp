"""战斗启发式的回归测试。

【为什么值得单独测】
这块逻辑打死过一整局（spec.md「6.2 血的教训」）。那次的三个缺陷现在都是硬性
要求，而要求会不会在下一次重构里被悄悄改回去，只有测试能保证。

用例尽量取**真实局面**：v1 死亡当时的两个回合、以及 2026-08-01 力士局里
实测到的几个局面。这样断言的是「不再犯那个错」，而不是我编出来的场景。

运行：python -m pytest src/mcp_server/test_autoplay.py -q
"""

from __future__ import annotations

import autoplay


def state(hp=60, max_hp=80, block=0, energy=3, enemies=(), hand=(), **kw):
    base = {
        "in_run": True,
        "in_combat": True,
        "awaiting_choice": False,
        "run": {"game_over": False},
        "player": {"hp": hp, "max_hp": max_hp, "block": block, "powers": []},
        "combat": {"turn": 1, "energy": energy},
        "enemies": list(enemies),
        "hand": list(hand),
    }
    base.update(kw)
    return base


def enemy(i, hp, damage=None, powers=(), alive=True, hittable=True, id="Mob", block=0):
    intents = [{"type": "Attack", "damage": damage, "total": damage}] if damage else [{"type": "Buff"}]
    return {"i": i, "id": id, "hp": hp, "block": block, "alive": alive, "hittable": hittable,
            "powers": list(powers), "intents": intents}


def card(i, id="StrikeIronclad", cost=1, type="Attack", target="AnyEnemy",
         playable=True, damage=None, block=None, damage_vs=None, values=None):
    v = dict(values or {})
    if damage is not None:
        v["Damage"] = damage
    if block is not None:
        v["Block"] = block
    c = {"i": i, "id": id, "cost": cost, "type": type, "target": target,
         "playable": playable, "values": v}
    if damage_vs is not None:
        c["damage_vs"] = damage_vs
    return c


def defend(i, block=5, cost=1):
    return card(i, "DefendIronclad", cost=cost, type="Skill", target="Self", block=block)


# --------------------------------------------------------------------------
#  安全线：v1 正是死在「不该接管却接管了」
# --------------------------------------------------------------------------


def test_v1_死亡局面必须交还():
    """HP 37/70 面对四只史莱姆 —— spec §6.2 记录的那一局。

    这属于 spec §4 的第 3 档（高价值决策），本地启发式**一张牌都不该打**。
    """
    s = state(hp=37, max_hp=70,
              enemies=[enemy(i, 20, damage=7) for i in range(4)],
              hand=[card(0, damage=6)])
    assert autoplay.handoff_reason(s) is not None


def test_血量低于四成交还():
    s = state(hp=31, max_hp=80, enemies=[enemy(0, 20, damage=3)], hand=[card(0, damage=6)])
    assert "血量过低" in autoplay.handoff_reason(s)


def test_预计掉血超过四分之一交还():
    """实测局面：HP 53/80、来袭 26 —— v2 在这里正确停手，接手后靠斩杀保住了血。"""
    s = state(hp=53, max_hp=80,
              enemies=[enemy(0, 31, damage=10), enemy(1, 16, damage=16)],
              hand=[card(0, damage=6)])
    assert "1/4" in autoplay.handoff_reason(s)


def test_致死风险优先于比例判据():
    """来袭 ≥ 血量时必须报致死，而不是笼统地报「掉血过多」。

    血量本身还在四成线以上（40/80），走不到「血量过低」那条。
    """
    s = state(hp=40, max_hp=80, enemies=[enemy(0, 50, damage=40)], hand=[card(0, damage=6)])
    assert "致死" in autoplay.handoff_reason(s)


def test_格挡计入净掉血():
    """已有格挡能把局面拉回安全线内 —— 否则会过度交还。"""
    risky = state(hp=60, max_hp=80, block=0, enemies=[enemy(0, 30, damage=20)], hand=[])
    safe = state(hp=60, max_hp=80, block=18, enemies=[enemy(0, 30, damage=20)], hand=[])
    assert autoplay.handoff_reason(risky) is not None
    assert autoplay.handoff_reason(safe) is None


def test_本回合还能叠的格挡也要算进去():
    """判定发生在回合开始，此刻 player.block 必然是 0。

    只看它 = 假定一点格挡都不打，于是每个「来袭超过血量 1/4」的普通回合都会
    交还。2026-08-01 实测：一场 39 血小怪的常规战斗第 3 回合就停手，
    而当时手里明明有 16 点格挡的血墙。
    """
    hand = [card(0, "BloodWall", cost=2, type="Skill", target="Self", block=16)]
    s = state(hp=47, max_hp=80, block=0, energy=3,
              enemies=[enemy(0, 30, damage=14)], hand=hand)
    assert autoplay.handoff_reason(s) is None
    # 同样的来袭，手上没有防御牌时照旧交还
    assert autoplay.handoff_reason(dict(s, hand=[card(0, damage=6)])) is not None


def test_叠得出格挡也救不了的局面照样交还():
    """放松判据不得放过 v1 那个死亡局面。"""
    s = state(hp=37, max_hp=70, energy=3,
              enemies=[enemy(i, 20, damage=7) for i in range(4)],
              hand=[defend(0), card(1, "Slimed", type="Status", target="Self")])
    assert autoplay.handoff_reason(s) is not None


def test_待答选择一律交还():
    s = state(awaiting_choice=True, enemies=[enemy(0, 20, damage=3)], hand=[card(0, damage=6)])
    assert "选择" in autoplay.handoff_reason(s)


def test_安全局面可以接管():
    s = state(hp=64, max_hp=80, enemies=[enemy(0, 25, damage=8)], hand=[card(0, damage=6)])
    assert autoplay.handoff_reason(s) is None


# --------------------------------------------------------------------------
#  要求 2：可打出 ≠ 值得打
# --------------------------------------------------------------------------


def test_绝不打废牌():
    """v1 在 12 血面对 22 点伤害时打了两张 Slimed，然后死了。"""
    s = state(enemies=[enemy(0, 30, damage=5)],
              hand=[card(0, "Slimed", type="Status", target="Self", values={"Damage": 0}),
                    card(1, "AscendersBane", type="Curse", target="Self"),
                    defend(2)])
    move, _ = autoplay.decide(s)
    assert move["card"] == 2      # 只会选那张防御


def test_不认识的牌不闷头结束回合():
    """要求 3：结束回合前必须确认无更优出牌。认不出的牌 = 可能更优。"""
    s = state(enemies=[enemy(0, 30, damage=5)],
              hand=[card(0, "FeelNoPain", cost=1, type="Power", target="Self", values={})])
    assert autoplay._unknown_affordable(s)


def test_条件伤害牌算攻击牌():
    """实测「完美打击」的 values 是 {CalculationBase, ExtraDamage, CalculatedDamage}，

    **没有 Damage 键**。只认 Damage 会把它当成不认识的牌白白交还 ——
    而它恰恰是牌组里伤害最高的那张。
    """
    perfected = card(0, "PerfectedStrike", cost=2,
                     values={"CalculationBase": 6, "ExtraDamage": 2, "CalculatedDamage": 22})
    s = state(enemies=[enemy(0, 22, damage=5)], hand=[perfected])
    assert not autoplay._unknown_affordable(s)
    move, why = autoplay.decide(s)
    assert move["card"] == 0
    assert "斩杀" in why          # 22 点正好清掉 22 血


def test_打不起的牌不算数():
    s = state(energy=0,
              enemies=[enemy(0, 30, damage=5)],
              hand=[card(0, "FeelNoPain", type="Power", target="Self",
                         playable=False, values={})])
    assert not autoplay._unknown_affordable(s)


# --------------------------------------------------------------------------
#  要求 1：格挡是硬约束
# --------------------------------------------------------------------------


def test_格挡先于输出():
    """有攻击牌也有防御牌、来袭挡不住时，先出防御。v1 正是在这里先打了攻击。"""
    s = state(hp=60, max_hp=80, energy=3,
              enemies=[enemy(0, 40, damage=12)],
              hand=[card(0, damage=6), defend(1)])
    move, why = autoplay.decide(s)
    assert move["card"] == 1
    assert "格挡" in why


def test_挡够了就转输出():
    s = state(hp=60, max_hp=80, block=15, energy=3,
              enemies=[enemy(0, 40, damage=12)],
              hand=[card(0, damage=6), defend(1)])
    move, why = autoplay.decide(s)
    assert move["card"] == 0
    assert "格挡已满足" in why


def test_没有防御牌时的理由必须说实话():
    """实测踩到：起手 5 张全是攻击牌，日志却写「格挡已满足」。

    决策日志写假理由比不写更糟 —— 6.5 的全部价值就在这个日志上。
    """
    s = state(hp=52, max_hp=80, block=0, energy=3,
              enemies=[enemy(0, 39, damage=8)],
              hand=[card(0, damage=9), card(1, damage=6)])
    _, why = autoplay.decide(s)
    assert "没有打得起的防御牌" in why
    assert "格挡已满足" not in why


def test_敌人不攻击时一点格挡都不叠():
    """strategy.md §1.3：格挡回合结束就清零，不攻击的回合叠格挡纯属浪费。"""
    s = state(enemies=[enemy(0, 40)], hand=[card(0, damage=6), defend(1)])
    move, _ = autoplay.decide(s)
    assert move["card"] == 0


# --------------------------------------------------------------------------
#  斩杀（strategy.md §1.1 / §1.2）
# --------------------------------------------------------------------------


def test_能斩杀就斩杀而不是堆格挡():
    """实测局面：怪 1 只剩 16 血却要打 16 点，杀掉它那 16 点当场消失。"""
    s = state(hp=60, max_hp=80, energy=3,
              enemies=[enemy(0, 31, damage=10, id="A"), enemy(1, 16, damage=16, id="B")],
              hand=[card(0, damage=6), card(1, damage=6), card(2, "Anger", cost=0, damage=6),
                    defend(3)])
    move, why = autoplay.decide(s)
    assert "斩杀" in why
    assert move["target"] == 1          # 打意图最高的那只


def test_斩杀用实际伤害而非卡面值():
    """damage_vs 含易伤修正。卡面 6 点杀不掉 8 血，实际 9 点杀得掉。"""
    s = state(enemies=[enemy(0, 8, damage=5)],
              hand=[card(0, damage=6, damage_vs=[9]), defend(1)])
    move, why = autoplay.decide(s)
    assert move["card"] == 0
    assert "斩杀" in why


def test_斩杀线要算上敌人的格挡():
    """实机抓到的（2026-08-01，第 9 层 SewerClam：56 血 + 8 格挡 + 每回合回满）。

    28 点伤害 ≥ 26 血，旧版据此判「斩杀，净挨 0」；实际两刀先被 8 点格挡
    吃掉，怪没死，那 14 点照样打在脸上。格挡先于血量被扣，斩杀线是血+格挡。
    """
    s = state(hp=60, max_hp=80, energy=3,
              enemies=[enemy(0, 26, block=8, damage=14, id="SewerClam")],
              hand=[card(0, "PerfectedStrike", cost=2, damage=22), card(1, damage=6), defend(2)])
    move, why = autoplay.decide(s)
    assert "斩杀" not in why          # 28 < 26 + 8，杀不掉，别装作杀得掉
    assert "格挡" in why              # 杀不掉就老老实实挡


def test_算上格挡仍杀得掉就照杀():
    s = state(hp=60, max_hp=80, energy=3,
              enemies=[enemy(0, 20, block=8, damage=14, id="SewerClam")],
              hand=[card(0, "PerfectedStrike", cost=2, damage=22), card(1, damage=6), defend(2)])
    move, why = autoplay.decide(s)
    assert "斩杀" in why


def test_斩杀不得绕过格挡硬约束():
    """杀得掉，但杀完剩下的能量挡不住其余敌人 —— 那就别杀，先挡。

    这条是 v1 死因的另一面：斩杀是优先项，但不能优先到把格挡挤掉。
    """
    s = state(hp=60, max_hp=80, energy=3,
              enemies=[enemy(0, 18, damage=4, id="小"), enemy(1, 60, damage=14, id="大")],
              hand=[card(0, damage=6), card(1, damage=6), card(2, damage=6), defend(3)])
    move, why = autoplay.decide(s)
    assert "格挡" in why


# --------------------------------------------------------------------------
#  荆棘：顺序决定挨不挨打（strategy.md §1.4）
# --------------------------------------------------------------------------


def test_当作防御牌打出的攻击牌也要带目标():
    """实机撞到的（2026-08-01，第 11 层）：铁斩波「5 点格挡 + 5 点伤害」。

    它是被当成防御牌选出来的，却是 AnyEnemy —— 下发时没带目标，被判
    bad_target，整场战斗就此交还。要不要目标由牌自己说了算，与我们为什么
    打它无关。
    """
    iron_wave = card(0, "IronWave", cost=1, type="Attack", target="AnyEnemy",
                     damage=5, block=5)
    s = state(hp=60, max_hp=80, energy=1,
              enemies=[enemy(0, 40, damage=9, id="小"), enemy(1, 40, damage=20, id="大")],
              hand=[iron_wave])
    move, why = autoplay.decide(s)
    assert move["card"] == 0
    assert move["target"] == 1          # 没有指定目标时，打威胁最大的那只


def test_不需要目标的牌不带目标():
    """反过来也要成立：AllEnemies 传了目标反而非法。"""
    s = state(hp=60, max_hp=80, energy=3,
              enemies=[enemy(0, 40, damage=9)],
              hand=[card(0, "Thunderclap", cost=1, target="AllEnemies", damage=4)])
    move, _ = autoplay.decide(s)
    assert "target" not in move


def test_有荆棘时先叠格挡():
    s = state(hp=60, max_hp=80, energy=3,
              enemies=[enemy(0, 40, damage=9, powers=[{"id": "ThornsPower", "amount": 2}])],
              hand=[card(0, damage=6), defend(1)])
    move, why = autoplay.decide(s)
    assert move["card"] == 1
    assert "荆棘" in why


# --------------------------------------------------------------------------
#  回合循环
# --------------------------------------------------------------------------


def test_触及安全线时一张牌都不打():
    """交还必须发生在**出牌之前** —— 这正是 v1 最根本的错误。"""
    played = []
    s = state(hp=20, max_hp=80, enemies=[enemy(0, 30, damage=5)], hand=[card(0, damage=6)])
    result = autoplay.play_combat(
        s,
        play_card=lambda c, t: played.append((c, t)) or {"ok": True, "state": s},
        end_turn=lambda: {"ok": True, "state": s},
    )
    assert result["stopped"] == "handoff"
    assert played == []


def test_战斗结束即停():
    s = state(enemies=[enemy(0, 5, damage=3)], hand=[card(0, damage=6)])
    ended = dict(s, in_combat=False)
    result = autoplay.play_combat(
        s,
        play_card=lambda c, t: {"ok": True, "state": ended},
        end_turn=lambda: {"ok": True, "state": ended},
    )
    assert result["stopped"] == "combat_ended"
    assert result["log"][0]["play"].startswith("StrikeIronclad")


def test_动作被拒绝时交还而不是重试():
    calls = []

    def reject(c, t):
        calls.append((c, t))
        return {"ok": False, "error": "unplayable", "reason": "EnergyCostTooHigh", "state": s}

    s = state(enemies=[enemy(0, 30, damage=5)], hand=[card(0, damage=6), defend(1)])
    result = autoplay.play_combat(s, play_card=reject, end_turn=lambda: {"ok": True, "state": s})
    assert result["stopped"] == "handoff"
    assert len(calls) == 1        # 只试了一次，没有重试
