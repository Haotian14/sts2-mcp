"""战斗规划：用手工构造的 /state 验证几条关键判断。"""

from __future__ import annotations

from agent.strategy.combat import plan_turn


def card(i, cid, cost=1, ctype="Attack", target="AnyEnemy", **values):
    return {"i": i, "id": cid, "cost": cost, "type": ctype, "target": target,
            "playable": True, "values": values}


def strike(i):
    return card(i, "StrikeSilent", Damage=6)


def defend(i):
    return card(i, "DefendSilent", ctype="Skill", target="Self", Block=5)


def enemy(i, hp, attack=0, block=0, powers=(), hits=1):
    intents = [{"type": "Attack", "damage": attack // hits, "total": attack, **({"repeats": hits} if hits > 1 else {})}] \
        if attack else [{"type": "Buff"}]
    return {"i": i, "id": f"Foe{i}", "hp": hp, "max_hp": hp, "block": block, "alive": True,
            "hittable": True, "intents": intents,
            "powers": [{"id": p, "amount": a} for p, a in powers]}


def state(hand, enemies, energy=3, hp=50, max_hp=70, block=0, potions=(None, None), encounter="TestWeak"):
    return {"in_run": True, "in_combat": True,
            "combat": {"energy": energy, "phase": "Play", "encounter": encounter},
            "player": {"character": "Silent", "hp": hp, "max_hp": max_hp, "block": block, "powers": []},
            "run": {"act": 1, "total_floor": 3}, "hand": hand, "enemies": enemies,
            "potions": list(potions), "deck": []}


def first_card(s):
    plan = plan_turn(s)
    assert plan.move is not None, plan.why()
    return next(c["id"] for c in s["hand"] if c["i"] == plan.move.get("card")), plan


def test_kill_beats_block():
    """斩杀优先：两刀打死它，它这一回合的 10 点就没了。"""
    s = state([strike(0), strike(1), defend(2)], [enemy(0, 12, attack=10)])
    cid, plan = first_card(s)
    assert cid == "StrikeSilent", plan.why()


def test_enemy_block_counts_toward_lethal():
    """斩杀线是血量 + 格挡：12 血 + 8 格挡两刀打不死，该叠格挡。"""
    s = state([strike(0), strike(1), defend(2)], [enemy(0, 12, attack=10, block=8)], energy=2)
    cid, plan = first_card(s)
    assert cid == "DefendSilent", plan.why()


def test_no_block_when_enemy_not_attacking():
    s = state([strike(0), defend(1), defend(2)], [enemy(0, 40)], energy=1)
    cid, plan = first_card(s)
    assert cid == "StrikeSilent", plan.why()


def test_block_against_big_hit():
    s = state([strike(0), defend(1), defend(2), defend(3)], [enemy(0, 60, attack=18)], hp=30)
    cid, plan = first_card(s)
    assert cid == "DefendSilent", plan.why()


def test_poison_kills_before_enemy_acts():
    """毒在敌人行动前结算：毒够就不用挡。"""
    s = state([card(0, "PoisonedStab", Damage=6, PoisonPower=3), defend(1)],
              [enemy(0, 9, attack=12, powers=[("PoisonPower", 1)])], energy=1)
    cid, plan = first_card(s)
    assert cid == "PoisonedStab", plan.why()
    assert plan.move["target"] == 0


def test_vulnerable_before_attacks():
    """先上易伤再打：痛击排在打击前面。"""
    hand = [card(0, "Bash", cost=2, Damage=8, VulnerablePower=2), card(1, "StrikeIronclad", Damage=6)]
    s = state(hand, [enemy(0, 80)], energy=3)
    s["player"]["character"] = "Ironclad"
    cid, plan = first_card(s)
    assert cid == "Bash", plan.why()


def test_block_potion_when_lethal_incoming():
    s = state([defend(0)], [enemy(0, 60, attack=24)], energy=1, hp=18, potions=("BlockPotion", None))
    plan = plan_turn(s)
    assert "potion" in (plan.move or {}) or "药水" in " ".join(plan.line), plan.why()


def test_blade_dance_shivs_for_lethal():
    """刀刃之舞生成的小刀在模拟里能打出去：3 刀 × 4 = 12 正好斩杀。"""
    hand = [card(0, "BladeDance", ctype="Skill", target="Self", Cards=3), defend(1)]
    s = state(hand, [enemy(0, 12, attack=14)], energy=1)
    cid, plan = first_card(s)
    assert cid == "BladeDance", plan.why()


def test_end_turn_when_nothing_useful():
    s = state([card(0, "AscendersBane", cost=None, ctype="Curse", target="None")],
              [enemy(0, 30, attack=5)])
    s["hand"][0]["playable"] = False
    assert plan_turn(s).move is None


def test_aoe_on_many_enemies():
    hand = [card(0, "DaggerSpray", target="AllEnemies", Damage=4, ), strike(1)]
    hand[0]["hits"] = 2
    s = state(hand, [enemy(0, 8, attack=5), enemy(1, 8, attack=5), enemy(2, 8, attack=5)], energy=1)
    cid, plan = first_card(s)
    assert cid == "DaggerSpray", plan.why()


def test_neutralize_first():
    """中和不花能量、挂虚弱：同分时先打，而不是留到最后。"""
    s = state([strike(0), strike(1), card(2, "Neutralize", cost=0, Damage=3, WeakPower=1)],
              [enemy(0, 60, attack=10)], energy=2)
    cid, plan = first_card(s)
    assert cid == "Neutralize", plan.why()


def test_tracking_doubles_after_weak():
    """有跟踪时，先挂虚弱让后面的攻击翻倍：3 + 6×2 + 6×2 = 27 ≥ 25，斩杀。"""
    s = state([strike(0), strike(1), card(2, "Neutralize", cost=0, Damage=3, WeakPower=1)],
              [enemy(0, 25, attack=10)], energy=2)
    s["player"]["powers"] = [{"id": "TrackingPower", "amount": 2}]
    plan = plan_turn(s)
    assert plan.line[0].startswith("中和") and plan.score > 40, plan.why()


def test_afterimage_before_other_cards():
    """残影先打：之后每张牌 +1 格挡。后打等于白白少挡几点。"""
    s = state([strike(0), card(1, "Afterimage", ctype="Power", target="Self", AfterimagePower=1),
               card(2, "BladeDance", ctype="Skill", target="Self", Cards=3)],
              [enemy(0, 60, attack=12)])
    cid, plan = first_card(s)
    assert cid == "Afterimage", plan.why()


def test_accuracy_before_shivs():
    """精准先打：手里的小刀每把 +4。"""
    shiv = lambda i: card(i, "Shiv", cost=0, Damage=4)
    s = state([shiv(0), shiv(1), shiv(2), card(3, "Accuracy", ctype="Power", target="Self", AccuracyPower=4)],
              [enemy(0, 60, attack=5)], energy=1)
    cid, plan = first_card(s)
    assert cid == "Accuracy", plan.why()


def test_existing_afterimage_counts_block():
    """已有残影时，多打一张 0 费小刀也多 1 格挡 —— 敌人打 3 点时，打完 3 把刀正好挡住。"""
    shiv = lambda i: card(i, "Shiv", cost=0, Damage=4)
    s = state([shiv(0), shiv(1), shiv(2)], [enemy(0, 60, attack=3)], energy=0)
    s["player"]["powers"] = [{"id": "AfterimagePower", "amount": 1}]
    plan = plan_turn(s)
    assert len(plan.line) == 3 and plan.score > 0, plan.why()


def test_draw_before_other_cards():
    """肾上腺素先打：抽牌加能量，先看到牌再决定怎么出。"""
    s = state([card(0, "Backstab", cost=0, Damage=11), defend(1),
               card(2, "Adrenaline", cost=0, ctype="Skill", target="Self", Cards=2, Energy=1)],
              [enemy(0, 60, attack=8)], energy=1)
    cid, plan = first_card(s)
    assert cid == "Adrenaline", plan.why()
