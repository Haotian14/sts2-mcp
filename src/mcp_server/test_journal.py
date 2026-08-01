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


def test_终局记录只补一次():
    journal.record(state(floor=12, game_over=True), "a", "")
    journal.record(state(floor=12, game_over=True), "b", "")
    assert sum(1 for e in lines() if e.get("kind") == "run_end") == 1


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
