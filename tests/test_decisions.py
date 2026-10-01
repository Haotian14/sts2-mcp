"""战斗外的决策：路线、选牌应答、奖励、休息点、事件，以及 runner 的分派。"""

from __future__ import annotations

from agent import runner
from agent.strategy import choices, rooms, route
from agent.strategy.deck import DeckContext

STARTER = ["StrikeSilent"] * 5 + ["DefendSilent"] * 5 + ["Neutralize", "Survivor"]


def base_state(**kw):
    s = {"in_run": True, "in_combat": False, "awaiting_choice": False,
         "run": {"act": 1, "floor": 3, "total_floor": 3, "gold": 99},
         "player": {"character": "Silent", "hp": 60, "max_hp": 70, "block": 0},
         "deck": list(STARTER), "relics": [], "potions": [None, None]}
    s.update(kw)
    return s


def two_way_map(first: str, second: str):
    # 起点 (1,0)/(1,1) 两条路，各自一个房间后汇合到休息点，再到 Boss
    nodes = [[1, 0, first, [[2, 0]]], [1, 1, second, [[2, 0]]],
             [2, 0, "RestSite", [[3, 0]]], [3, 0, "Boss", []]]
    return {"can_move": True, "coord": {"row": 0, "col": 0},
            "options": [{"i": 0, "row": 1, "col": 0, "type": first},
                        {"i": 1, "row": 1, "col": 1, "type": second}],
            "nodes": nodes}


def test_route_avoids_elite_when_low():
    s = base_state(map=two_way_map("Elite", "Monster"))
    s["player"]["hp"] = 20
    i, why = route.best_move(s)
    assert i == 1, why


def test_route_takes_elite_when_healthy_later():
    s = base_state(map=two_way_map("Elite", "Monster"))
    s["run"]["floor"] = 8
    s["player"]["hp"] = 70
    i, why = route.best_move(s)
    assert i == 0, why


def test_choice_discard_prefers_curse():
    s = base_state(in_combat=True, awaiting_choice=True, choice={
        "min": 1, "max": 1, "purpose": "FromHandForDiscard", "source": "Survivor",
        "options": [{"i": 0, "id": "StrikeSilent"}, {"i": 1, "id": "Regret"}, {"i": 2, "id": "Adrenaline"}]})
    idx, why = choices.answer(s, rooms.deck_context(s))
    assert idx == [1], why


def test_choice_upgrade_picks_best():
    s = base_state(awaiting_choice=True, choice={
        "min": 1, "max": 1, "purpose": "FromDeckForUpgrade",
        "options": [{"i": 0, "id": "StrikeSilent"}, {"i": 1, "id": "Adrenaline"}, {"i": 2, "id": "DefendSilent"}]})
    idx, why = choices.answer(s, rooms.deck_context(s))
    assert idx == [1], why


def test_choice_removal_by_hint():
    s = base_state(awaiting_choice=True, choice={
        "min": 1, "max": 1, "purpose": "FromDeckGeneric",
        "options": [{"i": 0, "id": "Adrenaline"}, {"i": 1, "id": "StrikeSilent"}]})
    idx, why = choices.answer(s, rooms.deck_context(s), hint="negative")
    assert idx == [1], why


def test_rewards_order_and_skip_memory():
    s = base_state(screen={"type": "NRewardsScreen", "can_proceed": True, "options": [
        {"i": 0, "id": "CardReward"}, {"i": 1, "id": "GoldReward"}]})
    assert rooms.rewards(s, skipped_cards=False).arg == 1
    s["screen"]["options"] = [{"i": 0, "id": "CardReward"}]
    assert rooms.rewards(s, skipped_cards=False).arg == 0
    assert rooms.rewards(s, skipped_cards=True).action == "proceed"


def test_card_reward_skip_when_all_bad():
    s = base_state(screen={"type": "NCardRewardSelectionScreen", "options": [
        {"i": 0, "id": "Slice"}, {"i": 1, "id": "Slice"}, {"i": 2, "id": "Alt:Skip"}]})
    s["deck"] = STARTER + ["Slice"] * 4 + ["Adrenaline"] * 12
    d = rooms.card_reward(s, rerolled=True)
    assert d.arg == 2, d.why


def test_rest_heals_when_low_upgrades_when_high():
    s = base_state(screen={"type": "NRestSiteRoom", "options": [
        {"i": 0, "id": "HEAL"}, {"i": 1, "id": "SMITH"}]})
    s["player"]["hp"] = 25
    assert rooms.rest_site(s).arg == 0
    s["player"]["hp"] = 65
    d = rooms.rest_site(s)
    assert d.arg == 1 and d.hint == "upgrade"


def test_event_prefers_relic_over_hp_loss():
    s = base_state(screen={"type": "NEventRoom", "options": [
        {"i": 0, "id": "E.pages.INITIAL.options.A", "title": "献祭", "text": "失去 30 点生命。"},
        {"i": 1, "id": "E.pages.INITIAL.options.B", "title": "拿走", "text": "获得一件遗物。", "relic": "Anchor"},
        {"i": 2, "id": "E.pages.INITIAL.options.C", "title": "离开", "text": ""}]})
    assert rooms.event(s).arg == 1


def test_runner_dispatch():
    s = base_state(screen={"type": "NTreasureRoom", "options": [{"i": 0, "id": "Chest"}]})
    assert runner.decide(s, runner.Memory()).action == "pick"
    over = base_state(run={"game_over": True})
    assert runner.decide(over, runner.Memory()).action == "stop"
    combat = base_state(in_combat=True, combat={"energy": 1, "phase": "Play"},
                        hand=[{"i": 0, "id": "StrikeSilent", "cost": 1, "type": "Attack",
                               "target": "AnyEnemy", "playable": True, "values": {"Damage": 6}}],
                        enemies=[{"i": 0, "id": "X", "hp": 5, "block": 0, "alive": True,
                                  "hittable": True, "intents": [{"type": "Attack", "total": 5}]}])
    d = runner.decide(combat, runner.Memory())
    assert d.action == "play_card" and d.arg == {"card": 0, "target": 0}


def test_deck_context_counts_upgrades():
    ctx = DeckContext("Silent", ["Adrenaline+", "StrikeSilent"])
    assert ctx.ids() == ["Adrenaline", "StrikeSilent"]


def test_payoff_needs_source():
    # 没有小刀源时，幻影之刃是空过的牌；有了刀刃之舞 / 忍者卷轴才值钱
    from agent.strategy.deck import value_in_deck
    bare = DeckContext("Silent", list(STARTER))
    with_cards = DeckContext("Silent", STARTER + ["BladeDance", "CloakAndDagger"])
    with_relic = DeckContext("Silent", list(STARTER), relics=["NinjaScroll"])
    v0 = value_in_deck("PhantomBlades", bare)
    assert value_in_deck("PhantomBlades", with_cards) > v0 + 0.8
    assert value_in_deck("PhantomBlades", with_relic) > v0 + 0.4
    # 来源件本身不受影响
    assert value_in_deck("BladeDance", bare) == value_in_deck("BladeDance", DeckContext("Silent", list(STARTER)))


def test_payoffs_do_not_count_each_other_as_synergy():
    from agent.strategy.deck import value_in_deck
    alone = DeckContext("Silent", list(STARTER))
    with_other_payoff = DeckContext("Silent", STARTER + ["Accuracy"])
    assert value_in_deck("PhantomBlades", with_other_payoff) <= value_in_deck("PhantomBlades", alone)


def test_defense_gap_lifts_block_cards():
    # 起始牌之外没有格挡时，同分段的防御牌应压过纯输出牌
    from agent.strategy.deck import value_in_deck
    bare = DeckContext("Silent", list(STARTER), act=2)
    stocked = DeckContext("Silent", STARTER + ["LegSweep", "Backflip", "CloakAndDagger", "DodgeAndRoll"], act=2)
    assert value_in_deck("EscapePlan", bare) > value_in_deck("EscapePlan", stocked) + 0.5


def test_route_takes_elite_once_deck_has_grown():
    # 两条路：前面先打两场小怪，再选精英还是问号。血量健康时应该打精英
    nodes = [[1, 0, "Monster", [[2, 0]]], [2, 0, "Monster", [[3, 0], [3, 1]]],
             [3, 0, "Elite", [[4, 0]]], [3, 1, "Unknown", [[4, 0]]],
             [4, 0, "RestSite", [[5, 0]]], [5, 0, "Boss", []]]
    m = {"can_move": True, "coord": {"row": 2, "col": 0},
         "options": [{"i": 0, "row": 3, "col": 0, "type": "Elite"},
                     {"i": 1, "row": 3, "col": 1, "type": "Unknown"}], "nodes": nodes}
    s = base_state(map=m)
    s["run"]["floor"] = 3
    s["player"]["hp"] = 55
    i, why = route.best_move(s)
    assert i == 0, why


def test_crystal_sphere_opens_cells_then_proceeds():
    cells = [{"i": k, "id": f"Cell:{x},{y}", "available": True}
             for k, (x, y) in enumerate((x, y) for y in range(3) for x in range(3))]
    s = base_state(screen={"type": "NCrystalSphereScreen", "options": cells, "can_proceed": False})
    d = runner.decide(s, runner.Memory())
    assert d.action == "pick" and d.arg == 4, d.why          # 正中 (1,1)
    for c in cells:
        c["available"] = False                                 # 占卜用完
    s["screen"]["can_proceed"] = True
    assert runner.decide(s, runner.Memory()).action == "proceed"


def test_event_gaining_curse_card_is_negative():
    # 水晶球：「获得一张债务」（诅咒，文本里没有「诅咒」二字）不该胜过付金币
    s = base_state(screen={"type": "NEventRoom", "options": [
        {"i": 0, "id": "CRYSTAL_SPHERE.pages.INITIAL.options.UNCOVER_FUTURE",
         "title": "揭幕未来", "text": "支付61金币。占卜3次。"},
        {"i": 1, "id": "CRYSTAL_SPHERE.pages.INITIAL.options.PAYMENT_PLAN",
         "title": "分期付款", "text": "获得一张债务。占卜6次。"}]})
    s["run"]["gold"] = 324
    d = rooms.event(s)
    assert d.arg == 0, d.why


def test_third_copy_penalized():
    from agent.strategy.deck import value_in_deck
    one = DeckContext("Silent", STARTER + ["Prepared"])
    two = DeckContext("Silent", STARTER + ["Prepared", "Prepared"])
    assert value_in_deck("Prepared", one) - value_in_deck("Prepared", two) > 0.6


def test_runner_ignores_game_over_left_from_previous_run():
    # 启动时停在上一局的终局界面：应当去开新局，而不是算作「打完 1 局」
    over = base_state(run={"game_over": True, "total_floor": 35}, screen={"type": "NGameOverScreen", "options": [
        {"i": 0, "id": "返回主菜单", "available": True}]})
    fresh = base_state(in_run=False, screen={"type": "NMainMenu", "options": []})

    class Stub:
        def __init__(self):
            self.calls = 0
        def state(self):
            return over if self.calls == 0 else fresh
        def act(self, *a, **k):
            self.calls += 1
            return {"ok": True, "state": fresh}

    out = runner.play(Stub(), max_steps=3, new_run="silent", max_runs=1)
    assert out["stopped"] != "已打完 1 局", out["stopped"]


def test_bundle_choice_picks_best_then_confirms():
    s = base_state(screen={"type": "NChooseABundleSelectionScreen", "options": [
        {"i": 0, "id": "Bundle:Slice|Slice|Slice", "available": True},
        {"i": 1, "id": "Bundle:Adrenaline|Backflip|Footwork", "available": True}]})
    d = runner.decide(s, runner.Memory())
    assert d.action == "pick" and d.arg == 1, d.why
    s["screen"]["options"].append({"i": 2, "id": "Confirm", "available": True})
    assert runner.decide(s, runner.Memory()).arg == 2


def test_discard_keeps_cards_planned_this_turn():
    # 投掷匕首要弃一张：敌人要打 12，手里防御能挡、致命毒药也要打 —— 弃打不出的那张
    hand = [{"i": 0, "id": "DeadlyPoison", "cost": 1, "type": "Skill", "target": "AnyEnemy",
             "playable": True, "values": {"PoisonPower": 5}},
            {"i": 1, "id": "DefendSilent", "cost": 1, "type": "Skill", "target": "Self",
             "playable": True, "values": {"Block": 5}},
            {"i": 2, "id": "Backflip", "cost": 1, "type": "Skill", "target": "Self",
             "playable": True, "values": {"Block": 5, "Cards": 2}}]
    s = base_state(in_combat=True, awaiting_choice=True, hand=hand,
                   combat={"energy": 2, "phase": "Play", "encounter": "TestWeak"},
                   enemies=[{"i": 0, "id": "X", "hp": 40, "block": 0, "alive": True, "hittable": True,
                             "intents": [{"type": "Attack", "total": 12}], "powers": []}],
                   choice={"min": 1, "max": 1, "purpose": "FromHandForDiscard", "source": "DaggerThrow",
                           "options": [{"i": 0, "id": "DeadlyPoison"}, {"i": 1, "id": "DefendSilent"},
                                       {"i": 2, "id": "Backflip"}]})
    idx, why = choices.answer(s, rooms.deck_context(s))
    planned = choices._planned_cards(s)
    dropped = s["choice"]["options"][idx[0]]["id"]
    assert planned[dropped] == 0, (why, planned)


def test_shop_buys_key_card_over_early_removal():
    s = base_state(screen={"type": "NMerchantRoom", "options": [
        {"i": 0, "id": "MerchantCardRemovalEntry", "cost": 75, "available": True},
        {"i": 1, "id": "CloakAndDagger", "cost": 50, "available": True}]})
    s["run"]["gold"] = 119
    d = rooms.shop(s, set())
    assert d.arg == 1, d.why
