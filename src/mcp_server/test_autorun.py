"""整局 runner 的回归测试（spec.md 6.4）。

【测什么】
这个 runner 唯一的风险是**它替模型做了不该做的决定**。上一局整局的卡牌奖励
都由脚本「拿第一张」，牌组因此打不动 Boss（strategy.md §3.1）—— 那正是这里
每一条「必须交还」用例要钉死的东西。

另一半是「动作没生效却一直转」：商店槽位曾经点了没反应、不报错也不改状态
（spec §3.4f）。runner 撞上这种情况必须停下来，而不是空转到上限。

运行：python -m pytest src/mcp_server/test_autorun.py -q
"""

from __future__ import annotations

import pytest

import autorun
import journal


@pytest.fixture(autouse=True)
def 隔离日志(tmp_path, monkeypatch):
    """runner 的用例不该碰仓库里真实的 logs/ —— has_plan 会读它、run_id 会写它。

    不再把 has_plan 桩死成 True：那样会让所有走 play_run 的用例都在「开局
    复盘」这个特性关闭的状态下跑，往后任何在第 1 层跑 play_run 的新用例都
    会默默继承这个假状态。这里只隔离日志文件位置（真实 has_plan 读写的是
    tmp_path，不是仓库里的 logs/），has_plan 本身用真实实现 —— 空日志对
    应「本局没记过 plan」，多数既有用例走的是 floor > 1 或不经 play_run，
    不受影响；真正需要 floor == 1 走 play_run 的用例见「开局先复盘」小节，
    那里会显式安排好日志内容。
    """
    monkeypatch.setattr(journal, "PATH", str(tmp_path / "decisions.jsonl"))
    monkeypatch.setattr(journal, "_RUN_FILE", str(tmp_path / "current-run.json"))
    monkeypatch.setattr(journal, "_warned", False)


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
    s = state(screen=screen(options=[opt(0, "PotionReward", available=False)], can_proceed=True))
    action, _, _ = autorun.decide(s)
    assert action == "proceed"


def test_药水栏满时跳过药水去拿遗物():
    """实测（第 8 层精英战后）：药水栏三格全满，奖励项的 available 仍是 true，
    点了没反应、状态不变，runner 空转到 stuck。空位得自己数。"""
    s = state(potions=["FlexPotion", "PowderedDemise", "FlexPotion"],
              screen=screen(options=[opt(0, "PotionReward"), opt(1, "RelicReward")]))
    action, arg, _ = autorun.decide(s)
    assert (action, arg) == ("pick", 1)


def test_界面上只剩拿不了的药水就按继续():
    """否则模型会被反复叫醒去处理一个根本领不了的奖励。"""
    s = state(potions=["a", "b", "c"],
              screen=screen(options=[opt(0, "PotionReward")], can_proceed=True))
    assert autorun.decide(s)[0] == "proceed"


def test_药水栏有空位就照拿():
    s = state(potions=["FlexPotion", None, None],
              screen=screen(options=[opt(0, "PotionReward")]))
    action, arg, _ = autorun.decide(s)
    assert (action, arg) == ("pick", 0)


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
    """整局的卡牌奖励都「拿第一张」，牌组因此打不动 Boss（strategy.md §3.1）。"""
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


def test_事件结算完那句继续算导航不算决策():
    """实测第 1 层涅奥事件：选完祝福后只剩一个「继续」。"""
    s = state(screen=screen("NEventRoom", [opt(0, "继续")]))
    action, arg, _ = autorun.decide(s)
    assert (action, arg) == ("pick", 0)


def test_只要还有第二个选项就不算导航():
    """事件里真正的选项从不叫「继续」，但保险起见：多于一个就交还。"""
    s = state(screen=screen("NEventRoom", [opt(0, "继续"), opt(1, "失去9点生命，选择一张牌变化")]))
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


def test_只有显式指定角色才跨过终局():
    dead = state(screen=screen("NGameOverScreen", [opt(0, "继续")]))
    dead["run"]["game_over"] = True
    assert autorun.decide(dead)[0] == "handoff"
    assert autorun.decide(dead, "Silent")[:2] == ("pick", 0)


def test_角色选择必须先选中再确认():
    options = [
        opt(0, "Ironclad", selected=False),
        opt(1, "Silent", selected=False),
        opt(2, "ConfirmButton"),
    ]
    menu = {"in_run": False, "screen": screen("NMainMenu", options)}
    assert autorun.decide(menu, "静默猎手")[:2] == ("pick", 1)
    options[1]["selected"] = True
    assert autorun.decide(menu, "Silent")[:2] == ("pick", 2)


def test_请求的角色不可用时停手而非确认默认角色():
    options = [opt(0, "Ironclad", selected=True), opt(1, "ConfirmButton")]
    menu = {"in_run": False, "screen": screen("NMainMenu", options)}
    action, _, why = autorun.decide(menu, "Silent")
    assert action == "handoff" and "Silent" in why


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


def test_终局一路开到指定角色的新局():
    dead_wait = state(screen=screen("NGameOverScreen", []))
    dead_wait["run"]["game_over"] = True
    dead_continue = state(screen=screen("NGameOverScreen", [opt(0, "继续")]))
    dead_continue["run"]["game_over"] = True
    dead_menu = state(screen=screen("NGameOverScreen", [opt(0, "返回主菜单")]))
    dead_menu["run"]["game_over"] = True
    single = {"in_run": False, "screen": screen("NMainMenu", [opt(0, "单人模式")])}
    standard = {"in_run": False, "screen": screen("NMainMenu", [opt(0, "标准模式")])}
    choose = {"in_run": False, "screen": screen("NMainMenu", [
        opt(0, "Ironclad", selected=False), opt(1, "Silent", selected=False),
        opt(2, "ConfirmButton")])}
    confirm = {"in_run": False, "screen": screen("NMainMenu", [
        opt(0, "Ironclad", selected=False), opt(1, "Silent", selected=True),
        opt(2, "ConfirmButton")])}
    # floor 特意用 2（而不是 1）：这条用例钉的是「跨局之后仍在第一处真正
    # 决策（NEventRoom 的二选一）停手」，不是「开局先复盘」那个停手点 ——
    # 若落在 floor == 1 且本局尚无计划，两个停手点会重叠，`stopped ==
    # "handoff"` 与角色断言恰好都还成立，测试却已经不再覆盖它本来要钉的
    # 决策点了（这正是 Important 4 指出的意图漂移）。跨局复盘的重新生效
    # 由另一条用例 `test_跨局连跑时开局复盘会重新生效` 专门覆盖。
    arrived = state(floor=2, screen=screen("NEventRoom", [opt(0, "领取祝福"), opt(1, "离开")]))
    arrived["player"]["character"] = "Silent"
    fake = Fake([dead_continue, dead_menu, single, standard, choose, confirm, arrived])
    result = autorun.play_run(
        dead_wait, play_card=lambda c, t: None, end_turn=lambda: None,
        pick=fake.pick, proceed=fake.proceed, move=fake.move,
        refresh=lambda: fake.states[0], settle_wait=0,
        new_run_character="Silent")
    assert fake.calls == ["pick0", "pick0", "pick0", "pick0", "pick1", "pick2"]
    assert result["stopped"] == "handoff"
    assert "领取祝福" in result["reason"] and "离开" in result["reason"]   # 停在事件二选一，不是复盘门
    assert result["state"]["player"]["character"] == "Silent"


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
    # 每次移动都真的往前走一层，指纹会变，故不会被 stuck 判据拦下。
    # 层数从 s 的初始层（5）往上加，不能从 1 起数 —— 现实中 total_floor
    # 只增不减，从 1 起数会让层数从 5 倒退到 1，不像真游戏；这也会撞上
    # 「开局先复盘」的 floor <= 1 检查，把这条用例变成误判成 handoff。
    walked = []

    def walk(i):
        walked.append(i)
        return {"ok": True, "state": state(
            floor=5 + len(walked), map_={"can_move": True, "options": [{"i": 0, "type": "Monster"}]})}

    fake.move = walk
    result = run(fake, max_steps=5)
    assert result["stopped"] == "max_steps"


# --------------------------------------------------------------------------
#  开局先复盘（spec.md 6.3b）
# --------------------------------------------------------------------------


ONE_ROAD = {"can_move": True, "options": [{"i": 0, "type": "Monster"}]}


def test_第一层且本局无计划就停手():
    action, _, why = autorun.decide(state(floor=1, map_=ONE_ROAD), has_plan=False)
    assert action == "handoff"
    assert "复盘" in why


def test_第一层已有计划就照常走():
    action, _, _ = autorun.decide(state(floor=1, map_=ONE_ROAD), has_plan=True)
    assert action == "move"


def test_默认不因缺计划停手():
    """不传 has_plan 时行为必须与从前完全一致 —— 老调用方一个都不能被绊住。"""
    action, _, _ = autorun.decide(state(floor=1, map_=ONE_ROAD))
    assert action == "move"


def test_半局接手不被复盘这条拦住():
    """第 5 层才开 runner 是常事，那时补写开局计划已无意义。"""
    action, _, _ = autorun.decide(state(floor=5, map_=ONE_ROAD), has_plan=False)
    assert action == "move"


def test_战斗中不因缺计划打断():
    """第 1 层直接开打时，停手要等这场打完 —— 半场撂挑子比漏一次复盘更糟。"""
    action, _, _ = autorun.decide(state(floor=1, in_combat=True), has_plan=False)
    assert action == "combat"


def test_经play_run时开局无计划真的会停手(monkeypatch):
    """5 条 decide 级用例都发现不了 play_run 把 has_plan 写死的情况 ——
    这条走完整路径，钉住「auto_run 真的会停」。"""
    monkeypatch.setattr(journal, "has_plan", lambda state: False)
    s = state(floor=1, map_=ONE_ROAD)
    fake = Fake([s])
    result = run(fake)
    assert result["stopped"] == "handoff"
    assert "复盘" in result["reason"]


def test_真实has_plan接线_空日志第一层停手_记完计划后不再停():
    """端到端钉住 journal 那一侧算得对不对 —— 之前所有经过 play_run 的用例
    都把 journal.has_plan 整个换成 lambda，journal 自己的判断从没被真正
    跑过一次。这里用的是真实 has_plan（日志走 fixture 隔离出的 tmp_path）。"""
    s = state(floor=1, map_=ONE_ROAD)
    fake = Fake([s])
    result = run(fake)
    assert result["stopped"] == "handoff"
    assert "复盘" in result["reason"]

    journal.record_plan(s, "这一局先探一条路试试")
    fake2 = Fake([s])
    result2 = run(fake2)
    assert fake2.calls == ["move0"]           # 不再因缺计划停手，径直往下走
    assert "复盘" not in result2["reason"]


def test_跨局连跑时开局复盘会重新生效():
    """入口那一局已有计划（entry 处 has_plan 会算出 True），但用
    new_run_character 连跑进新局后，新局第 1 层必须重新算一次 has_plan ——
    不能把入口时的结果一直背到新局头上（这正是这个特性最该生效的场景）。"""
    journal.record_plan(state(floor=1), "上一局的开局计划")
    journal.record(state(floor=9), "打到第9层", "")

    # floor 要接到上面记的第 9 层 —— 用默认的 5 会被 journal 判成「总层数
    # 倒退」，凭空造出另一局，入口 has_plan 就测不到「上一局已有计划」了。
    dead_wait = state(floor=9, screen=screen("NGameOverScreen", []))
    dead_wait["run"]["game_over"] = True
    dead_continue = state(screen=screen("NGameOverScreen", [opt(0, "继续")]))
    dead_continue["run"]["game_over"] = True
    dead_menu = state(screen=screen("NGameOverScreen", [opt(0, "返回主菜单")]))
    dead_menu["run"]["game_over"] = True
    single = {"in_run": False, "screen": screen("NMainMenu", [opt(0, "单人模式")])}
    standard = {"in_run": False, "screen": screen("NMainMenu", [opt(0, "标准模式")])}
    choose = {"in_run": False, "screen": screen("NMainMenu", [
        opt(0, "Ironclad", selected=False), opt(1, "Silent", selected=False),
        opt(2, "ConfirmButton")])}
    confirm = {"in_run": False, "screen": screen("NMainMenu", [
        opt(0, "Ironclad", selected=False), opt(1, "Silent", selected=True),
        opt(2, "ConfirmButton")])}
    arrived = state(floor=1, screen=screen("NEventRoom", [opt(0, "领取祝福"), opt(1, "离开")]))
    arrived["player"]["character"] = "Silent"
    fake = Fake([dead_continue, dead_menu, single, standard, choose, confirm, arrived])
    result = autorun.play_run(
        dead_wait, play_card=lambda c, t: None, end_turn=lambda: None,
        pick=fake.pick, proceed=fake.proceed, move=fake.move,
        refresh=lambda: fake.states[0], settle_wait=0,
        new_run_character="Silent")
    assert result["stopped"] == "handoff"
    assert "复盘" in result["reason"]


def test_日志写不进去时不会卡在开局复盘(monkeypatch):
    """Important 1：日志可读却写不进去（磁盘只读/满/ACL 限制）时，
    `record_plan` 会静默写失败，`has_plan` 若只处理「读不到」这一半，
    就会恒为 False——`auto_run` 每次都会在第 1 层停手要求复盘，而
    `set_plan` 又无条件回报成功，模型会以为记下来了，实际上永远过不去，
    陷入死循环。这里钉住 `play_run` 这一层的最终效果：写坏了也不能卡住。
    """
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if str(path) == journal.PATH and "a" in mode:
            raise OSError("模拟磁盘写入失败")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)

    s = state(floor=1, map_=ONE_ROAD)
    fake = Fake([s])
    result = run(fake)
    # 没有被开局复盘拦住——既然计划注定存不下来，停下去没有意义，
    # 径直往下走地图唯一的那条路。
    assert fake.calls == ["move0"]
    assert "复盘" not in result["reason"]
