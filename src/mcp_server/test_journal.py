"""决策日志的回归测试（spec.md 6.5）。

【测什么】
两件事最容易悄悄坏掉，且坏了当场看不出来：

1. **局的边界**。判错就等于把两局的记录混成一份，跨局复盘会得出错误结论；
   而 MCP server 会重启、一局要打几个小时，所以边界必须靠盘上的文件判，
   不能靠内存。
2. **记日志不能弄坏一步棋**。磁盘满了、路径没权限，顶多丢记录，绝不能让
   一个已经下发的动作变成失败回报。

运行：python -m pytest src/mcp_server/test_journal.py -q
"""

from __future__ import annotations

import json
import os

import pytest

import journal


@pytest.fixture(autouse=True)
def tmp_journal(tmp_path, monkeypatch):
    """每个用例一份独立的日志文件 —— 别把测试数据混进真实的跨局记录里。"""
    monkeypatch.setattr(journal, "PATH", str(tmp_path / "decisions.jsonl"))
    monkeypatch.setattr(journal, "_RUN_FILE", str(tmp_path / "current-run.json"))
    monkeypatch.setattr(journal, "_warned", False)
    return tmp_path


def state(floor=1, character="Ironclad", ascension=2, hp=70, gold=99,
          game_over=False, **kw):
    base = {
        "in_run": True,
        "in_combat": False,
        "run": {"act": 1, "total_floor": floor, "ascension": ascension,
                "gold": gold, "game_over": game_over, "room": "CombatRoom"},
        "player": {"character": character, "hp": hp, "max_hp": 80},
    }
    base.update(kw)
    return base


def lines():
    with open(journal.PATH, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


# --------------------------------------------------------------------------
#  写
# --------------------------------------------------------------------------


def test_记下局面动作与理由():
    journal.record(state(floor=7, hp=59, gold=66), "pick 剑柄打击", "上一局输出不足")
    (entry,) = lines()
    assert entry["floor"] == 7 and entry["hp"] == 59 and entry["gold"] == 66
    assert entry["action"] == "pick 剑柄打击"
    assert entry["why"] == "上一局输出不足"
    assert entry["by"] == "model"


def test_追加而不覆盖():
    journal.record(state(), "a", "")
    journal.record(state(), "b", "")
    assert [e["action"] for e in lines()] == ["a", "b"]


def test_战斗内标注回合数():
    s = state(in_combat=True, combat={"turn": 3, "energy": 3})
    journal.record(s, "play_card 打击", "斩杀", by="heuristic")
    assert lines()[0]["where"] == "combat T3"
    assert lines()[0]["by"] == "heuristic"


def test_界面名进_where():
    s = state(screen={"type": "NMerchantRoom", "options": []})
    journal.record(s, "pick 无情猛攻（38金）", "补输出")
    assert lines()[0]["where"] == "NMerchantRoom"


# --------------------------------------------------------------------------
#  取名
# --------------------------------------------------------------------------


def test_名字优先取中文标题():
    opts = [{"i": 0, "id": "Thunderclap", "title": "闪电霹雳"}]
    assert journal.option_name(opts, 0) == "闪电霹雳"


def test_商店选项带上金价():
    opts = [{"i": 0, "id": "Unrelenting", "title": "无情猛攻", "cost": 38}]
    assert journal.option_name(opts, 0) == "无情猛攻（38金）"


def test_待答选牌的cost是能量不是金价():
    """实机记出过一句假话：「choose 痛击+（2金）」，那个 2 是能量费用。

    判据是待答选牌的选项带卡牌 type，商店选项没有。
    """
    opts = [{"i": 0, "id": "Bash", "title": "痛击+", "cost": 2, "type": "Attack"}]
    assert journal.option_name(opts, 0) == "痛击+"


def test_找不到选项就退回下标():
    assert journal.option_name([], 3) == "#3"


# --------------------------------------------------------------------------
#  局的边界
# --------------------------------------------------------------------------


def test_同一局内层数推进不换局():
    journal.record(state(floor=1), "a", "")
    journal.record(state(floor=7), "b", "")
    assert len({e["run"] for e in lines()}) == 1


def test_层数倒退即为新的一局():
    journal.record(state(floor=9), "a", "")
    journal.record(state(floor=1), "b", "")
    a, b = lines()
    assert a["run"] != b["run"]


def test_换角色即为新的一局():
    journal.record(state(character="Ironclad"), "a", "")
    journal.record(state(character="Silent"), "b", "")
    assert lines()[0]["run"] != lines()[1]["run"]


def test_局的身份跨进程存活():
    """MCP server 重启不该把一局劈成两半 —— 身份存在盘上，不在内存里。"""
    journal.record(state(floor=3), "a", "")
    first = lines()[0]["run"]

    # 模拟重启：清掉进程内的一切，只留盘上的文件
    journal._warned = False
    journal.record(state(floor=4), "b", "")
    assert lines()[1]["run"] == first


def test_结束之后是新的一局_哪怕层数相同():
    """死在第 1 层、重开又停在第 1 层 —— 层数没倒退，靠 game_over 判。"""
    journal.record(state(floor=1, game_over=True), "死了", "")
    journal.record(state(floor=1), "新局第一步", "")
    entries = lines()
    assert entries[-1]["run"] != entries[0]["run"]


def test_结束时补一条终局记录():
    journal.record(state(floor=12, hp=0, game_over=True), "auto_combat 停手", "本局已结束")
    kinds = [e.get("kind") for e in lines()]
    assert "run_end" in kinds
    assert lines()[-1]["floor"] == 12


def test_死亡发生在动作之后_要靠observe补上():
    """实测（第 17 层 Boss 战）：最后一次 end_turn 记的是出手前的局面，
    `game_over` 仍是 false，于是人死了、日志却显示这局还在进行中。
    动作做完必须拿**执行后**的状态再看一眼。"""
    journal.record(state(floor=17, hp=11), "end_turn", "没牌可打了")
    assert not any(e.get("kind") == "run_end" for e in lines())

    journal.observe(state(floor=17, hp=0, game_over=True))
    end = [e for e in lines() if e.get("kind") == "run_end"]
    assert len(end) == 1 and end[0]["floor"] == 17
    assert end[0]["run"] == lines()[0]["run"]      # 与本局同号，不是新开一局


def test_observe不记决策():
    journal.observe(state(floor=3))
    assert not os.path.exists(journal.PATH)      # 一个字节都没写


def test_终局记录只补一次():
    journal.record(state(floor=12, game_over=True), "a", "")
    journal.record(state(floor=12, game_over=True), "b", "")
    assert sum(1 for e in lines() if e.get("kind") == "run_end") == 1


def test_主菜单上的点击不算一局():
    """开新局前要在主菜单点好几下，那几下不属于任何一局 —— 早先每次都会
    凭空造出一个 `…-unknown` 的局，混在真实战绩里。"""
    journal.record({"in_run": False, "screen": {"type": "NMainMenu"}}, "pick 单人模式", "")
    journal.record(state(floor=1), "pick 涅奥骨骰", "开局祝福")
    menu, first = lines()
    assert menu["run"] == "menu"
    assert first["run"] != "menu"


def test_读不到层数不算新局():
    """主菜单、游戏结束界面都读不到层数，那不代表换了一局。"""
    journal.record(state(floor=5), "a", "")
    s = state()
    s["run"]["total_floor"] = None
    journal.record(s, "b", "")
    assert lines()[0]["run"] == lines()[1]["run"]


# --------------------------------------------------------------------------
#  日志绝不能弄坏一步棋
# --------------------------------------------------------------------------


def test_写不进去也不抛异常(monkeypatch):
    monkeypatch.setattr(journal, "PATH", "Z:/根本不存在的盘/decisions.jsonl")
    monkeypatch.setattr(journal, "_RUN_FILE", "Z:/根本不存在的盘/current-run.json")
    journal.record(state(), "pick 某张牌", "理由")     # 不抛即通过


def test_空状态也不抛():
    journal.record({}, "pick #0", "")
    assert lines()[0]["action"] == "pick #0"


def test_半行不会毁掉整份日志():
    journal.record(state(), "a", "")
    with open(journal.PATH, "a", encoding="utf-8") as f:
        f.write('{"t":"被杀进程截断的半行"')
    journal.record(state(), "b", "")
    d = journal.digest()
    assert [x["action"] for x in d["runs"][0]["decisions"]] == ["a", "b"]


# --------------------------------------------------------------------------
#  读
# --------------------------------------------------------------------------


def test_摘要按局分组且只取最近几局():
    # 层数递减 —— 每一步都是「倒退」，故是四局而非一局。
    # （反过来 1 → 9 是同一局在往上爬，见 test_同一局内层数推进不换局）
    journal.record(state(floor=9), "第一局", "")
    journal.record(state(floor=5), "第二局", "")
    journal.record(state(floor=3), "第三局", "")
    journal.record(state(floor=1), "第四局", "")
    d = journal.digest(runs=2)
    assert [r["decisions"][0]["action"] for r in d["runs"]] == ["第三局", "第四局"]


def test_摘要丢掉战斗内的逐张出牌_留下构筑与拐点():
    combat = state(in_combat=True, combat={"turn": 1, "energy": 3})
    journal.record(combat, "play_card 打击", "常规输出", by="heuristic")
    journal.record(combat, "auto_combat 停手（handoff）", "血量过低",
                   by="heuristic", kind="stop")
    journal.record(combat, "play_card 血墙", "模型接手后自己打的", by="model")
    journal.record(state(screen={"type": "NRewardsScreen"}), "pick 剑柄打击", "补输出")

    actions = [x["action"] for x in journal.digest()["runs"][0]["decisions"]]
    assert "play_card 打击" not in actions            # 常规操作，价值低
    assert "auto_combat 停手（handoff）" in actions     # 启发式的边界在哪
    assert "play_card 血墙" in actions                 # 模型亲自出手的拐点
    assert "pick 剑柄打击" in actions                  # 构筑决策


def test_摘要给出到达层数与是否已结束():
    journal.record(state(floor=3), "a", "")
    journal.record(state(floor=11, game_over=True), "b", "")
    (run,) = journal.digest()["runs"]
    assert run["floors"] == 11 and run["ended"] is True


def test_没有日志文件时摘要为空():
    assert journal.digest()["runs"] == []


# --------------------------------------------------------------------------
#  开局复盘（spec.md 6.3b）
# --------------------------------------------------------------------------


def test_没有上一局时brief为空():
    assert journal.brief()["run"] is None


def test_只有菜单桶时视同没有上一局():
    journal.record({"in_run": False, "screen": {"type": "NMainMenu"}}, "pick 单人模式", "")
    assert journal.brief()["run"] is None


def test_brief取的是上一局而不是本局():
    journal.record(state(floor=9, character="Ironclad"), "上一局最后一步", "")
    journal.record(state(floor=1, character="Silent"), "本局第一步", "")
    b = journal.brief()
    assert b["character"] == "Ironclad"
    assert b["floor"] == 9


def test_brief给出死亡层数与死时血量():
    journal.record(state(floor=17, hp=11), "end_turn", "没牌可打了")
    journal.observe(state(floor=17, hp=0, game_over=True))
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    b = journal.brief()
    assert b["ended"] is True
    assert b["floor"] == 17 and b["hp"] == 0


def test_brief给出停手点分组与最后几条理由():
    combat = state(in_combat=True, combat={"turn": 4})
    journal.record(combat, "auto_combat 停手", "血量过低", by="heuristic", kind="stop")
    journal.record(state(screen={"type": "NMerchantRoom"}), "auto_run 停手", "商店要挑",
                   by="heuristic", kind="stop")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    b = journal.brief()
    assert b["stops"] == {"combat T4": 1, "NMerchantRoom": 1}
    assert "血量过低" in b["stop_reasons"]


def test_brief只收模型亲自做的构筑决策():
    journal.record(state(screen={"type": "NRewardsScreen"}), "pick 剑柄打击", "补输出")
    journal.record(state(in_combat=True, combat={"turn": 2}), "play_card 打击", "常规",
                   by="heuristic")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    b = journal.brief()
    assert [x["action"] for x in b["builds"]] == ["pick 剑柄打击"]


def test_brief带出上一局写下的计划():
    journal.record_plan(state(floor=1), "这一局优先拿降费件")
    journal.record(state(floor=5), "往下打", "")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    assert journal.brief()["plan"] == "这一局优先拿降费件"


def test_上一局没有终局记录时降级为见过的最大层数():
    journal.record(state(floor=3), "a", "")
    journal.record(state(floor=8), "b", "")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    b = journal.brief()
    assert b["ended"] is False and b["floor"] == 8


def test_brief读不到日志也不抛(monkeypatch):
    monkeypatch.setattr(journal, "PATH", "Z:/根本不存在的盘/decisions.jsonl")
    assert journal.brief()["run"] is None
