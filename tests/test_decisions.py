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
