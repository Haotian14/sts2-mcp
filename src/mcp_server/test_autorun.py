"""整局 runner 的回归测试（spec.md 6.4）。

【测什么】
这个 runner 唯一的风险是**它替模型做了不该做的决定**。上一局整局的卡牌奖励
都由脚本「拿第一张」，牌组因此打不动 Boss（strategy.md §5）—— 那正是这里
每一条「必须交还」用例要钉死的东西。

另一半是「动作没生效却一直转」：商店槽位曾经点了没反应、不报错也不改状态
（spec §3.4f）。runner 撞上这种情况必须停下来，而不是空转到上限。

运行：python -m pytest src/mcp_server/test_autorun.py -q
"""

from __future__ import annotations

import autorun


def state(floor=5, hp=60, gold=99, in_combat=False, screen=None, map_=None, **kw):
    base = {
        "in_run": True,
        "in_combat": in_combat,
        "awaiting_choice": False,
        "run": {"act": 1, "total_floor": floor, "gold": gold, "game_over": False},
        "player": {"character": "Ironclad", "hp": hp, "max_hp": 80, "block": 0},
        "map": map_ or {"can_move": False, "options": []},
    }
    if screen is not None:
        base["screen"] = screen
    base.update(kw)
    return base


def screen(type="NRewardsScreen", options=(), can_proceed=False):
    return {"type": type, "options": list(options), "can_proceed": can_proceed}


def opt(i, id, available=True, **kw):
    return {"i": i, "id": id, "available": available, **kw}


# --------------------------------------------------------------------------
#  只做唯一解
# --------------------------------------------------------------------------


def test_金币永远该拿():
    action, arg, why = autorun.decide(
        state(screen=screen(options=[opt(0, "GoldReward"), opt(1, "CardReward")], can_proceed=True)))
    assert (action, arg) == ("pick", 0)


def test_遗物与药水也该拿():
    for reward in ("RelicReward", "PotionReward"):
        action, arg, _ = autorun.decide(state(screen=screen(options=[opt(0, reward)])))
        assert (action, arg) == ("pick", 0), reward


def test_领不了的药水不去点():
    """药水栏满时 available 为 false，点它没有意义。"""
    s = state(screen=screen(options=[opt(0, "PotionReward", available=False)], can_proceed=True))
    action, _, _ = autorun.decide(s)
    assert action == "proceed"


def test_界面处理完按继续():
    action, _, _ = autorun.decide(state(screen=screen(options=[], can_proceed=True)))
    assert action == "proceed"


def test_地图只有一条路就走():
    s = state(map_={"can_move": True, "options": [{"i": 0, "type": "Monster"}]})
    action, arg, _ = autorun.decide(s)
    assert (action, arg) == ("move", 0)


def test_战斗交给启发式():
    action, _, _ = autorun.decide(state(in_combat=True))
    assert action == "combat"


def test_宝箱先开箱再拿遗物():
    s = state(screen=screen("NTreasureRoom", [opt(0, "Chest")]))
    assert autorun.decide(s)[0] == "pick"

    s = state(screen=screen("NTreasureRoom", [opt(0, "Whetstone", title="磨刀石")]))
    action, arg, why = autorun.decide(s)
    assert (action, arg) == ("pick", 0)
    assert "遗物" in why


# --------------------------------------------------------------------------
#  必须交还：上一局就是死在这些地方被脚本代拿了主意
# --------------------------------------------------------------------------


def test_卡牌三选一交还():
    """整局的卡牌奖励都「拿第一张」，牌组因此打不动 Boss（strategy.md §5）。"""
    s = state(screen=screen("NCardRewardSelectionScreen",
                            [opt(0, "Havoc"), opt(1, "Armaments"), opt(2, "Cinder")]))
    assert autorun.decide(s)[0] == "handoff"


def test_奖励界面只剩卡牌时交还而不是按继续():
    s = state(screen=screen(options=[opt(0, "CardReward")], can_proceed=True))
    assert autorun.decide(s)[0] == "handoff"


def test_商店交还():
    s = state(screen=screen("NMerchantRoom",
                            [opt(0, "Unrelenting", title="无情猛攻", cost=38)]))
    assert autorun.decide(s)[0] == "handoff"


def test_休息点交还():
    s = state(screen=screen("NRestSiteRoom", [opt(0, "HEAL"), opt(1, "FORGE")]))
    assert autorun.decide(s)[0] == "handoff"


def test_事件交还():
    s = state(screen=screen("NEventRoom", [opt(0, "失去34金币，获得2瓶随机药水")]))
    assert autorun.decide(s)[0] == "handoff"


def test_岔路交还():
    s = state(map_={"can_move": True,
                    "options": [{"i": 0, "type": "Monster"}, {"i": 1, "type": "Shop"}]})
    action, _, why = autorun.decide(s)
    assert action == "handoff" and "2 条路" in why


def test_精英与Boss就算只有一条路也交还():
    for kind in ("Elite", "Boss"):
        s = state(map_={"can_move": True, "options": [{"i": 0, "type": kind}]})
        action, _, why = autorun.decide(s)
        assert action == "handoff", kind
        assert kind in why


def test_待答的选择交还():
    assert autorun.decide(state(awaiting_choice=True))[0] == "handoff"


def test_认不出的界面不乱点():
    """判成 unclear 而非 handoff：可能只是还没到位，由 play_run 重读几次再说。"""
    s = state(screen=screen("NSomethingNew", [], can_proceed=False))
    action, _, why = autorun.decide(s)
    assert action == "unclear" and "NSomethingNew" in why


def test_局面结束与不在局中都交还():
    dead = state()
    dead["run"]["game_over"] = True
    assert autorun.decide(dead)[0] == "handoff"

    assert autorun.decide({"in_run": False})[0] == "handoff"


# --------------------------------------------------------------------------
#  循环
# --------------------------------------------------------------------------


class Fake:
    """一台按脚本回放状态的假游戏。"""

    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    def _next(self, what):
        self.calls.append(what)
        if self.states:
            self.states.pop(0)
        return {"ok": True, "state": self.states[0] if self.states else {"in_run": False}}

    def pick(self, i):
        return self._next(f"pick{i}")

    def proceed(self):
        return self._next("proceed")

    def move(self, i):
        return self._next(f"move{i}")


def run(fake, **kw):
    return autorun.play_run(
        fake.states[0], play_card=lambda c, t: None, end_turn=lambda: None,
        pick=fake.pick, proceed=fake.proceed, move=fake.move, **kw)


def test_一路跑到需要决策为止():
    fake = Fake([
        state(screen=screen(options=[opt(0, "GoldReward"), opt(1, "CardReward")], can_proceed=True)),
        state(gold=120, screen=screen(options=[opt(0, "CardReward")], can_proceed=True)),
    ])
    result = run(fake)
    assert fake.calls == ["pick0"]
    assert result["stopped"] == "handoff"
    assert "CardReward" in result["reason"]                 # 理由要点名等着挑的是什么
    assert result["log"][0]["do"] == "pick GoldReward"      # 记名字，不记下标


def test_动作被拒绝就停手():
    fake = Fake([state(screen=screen(options=[opt(0, "GoldReward")]))])
    fake.pick = lambda i: {"ok": False, "error": "bad_index", "state": fake.states[0]}
    result = run(fake)
    assert result["stopped"] == "rejected"


def test_局面毫无变化时停下来而不是空转():
    """点了没反应、不报错也不改状态 —— 商店槽位真的这样过（spec §3.4f）。"""
    stuck = state(screen=screen(options=[opt(0, "GoldReward")]))
    fake = Fake([stuck])
    fake.pick = lambda i: {"ok": True, "state": stuck}
    result = run(fake, max_steps=50)
    assert result["stopped"] == "stuck"
    assert result["steps"] < 10


def test_界面没到位时再看一眼而不是当场交还():
    """实机撞到（第 11 层）：战斗打完，奖励界面还没浮出来，此刻是
    NCombatRoom + 零选项 + 不能继续 —— 照字面判就是「卡住」，
    而 0.8 秒后奖励界面就到了。"""
    半路 = state(screen=screen("NCombatRoom", [], can_proceed=False))
    到位 = state(screen=screen(options=[opt(0, "GoldReward")], can_proceed=True))
    fake = Fake([半路])
    result = autorun.play_run(
        半路, play_card=lambda c, t: None, end_turn=lambda: None,
        pick=fake.pick, proceed=fake.proceed, move=fake.move,
        refresh=lambda: 到位, settle_wait=0)
    assert fake.calls == ["pick0"]        # 等到了，照常领奖


def test_等几次仍是如此才交还():
    卡住 = state(screen=screen("NCombatRoom", [], can_proceed=False))
    fake = Fake([卡住])
    looks = []
    result = autorun.play_run(
        卡住, play_card=lambda c, t: None, end_turn=lambda: None,
        pick=fake.pick, proceed=fake.proceed, move=fake.move,
        refresh=lambda: (looks.append(1), 卡住)[1], settle_wait=0)
    assert result["stopped"] == "handoff"
    assert len(looks) == autorun.UNCLEAR_RETRIES      # 有限次，不会一直等下去


def test_走满上限也会停():
    s = state(map_={"can_move": True, "options": [{"i": 0, "type": "Monster"}]})
    fake = Fake([s])
    # 每次移动都真的往前走一层，指纹会变，故不会被 stuck 判据拦下
    walked = []

    def walk(i):
        walked.append(i)
        return {"ok": True, "state": state(
            floor=len(walked), map_={"can_move": True, "options": [{"i": 0, "type": "Monster"}]})}

    fake.move = walk
    result = run(fake, max_steps=5)
    assert result["stopped"] == "max_steps"
