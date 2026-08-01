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

import journal
from conftest import _block_append


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


def test_brief在刚死还没开新局时返回刚打完那局而不是上上局():
    """Important 1：`current-run.json` 里的标识在「刚死、还没开新局」这个
    窗口指向的正是**刚打完的那一局**（`ended` 为 true）。修复前 `brief()`
    只要看到 `current-run.json` 里有个 id 就整个当「当前局」排除掉，于是
    死亡界面/主菜单上调用会跳过刚死的这局，错把上上局的事实当成「上一局」
    返回——且没有任何报错或提示。

    只有当那一局**尚未结束**时，才该把它当「当前局」排除；已经结束的那局
    本身就是「上一局」，应当被 brief() 返回。"""
    # 上上局 A：打到第 5 层后结束
    journal.record(state(floor=5, character="Ironclad"), "A局第一步", "")
    journal.observe(state(floor=5, character="Ironclad", hp=0, game_over=True))
    # 上一局 B：打到第 3 层后结束——此刻正是「刚死，还没开新局」的窗口，
    # current-run.json 记的就是 B，且 ended 为 true。
    journal.record(state(floor=3, character="Silent"), "B局第一步", "")
    journal.observe(state(floor=3, character="Silent", hp=0, game_over=True))

    b = journal.brief()
    assert b["character"] == "Silent"
    assert b["floor"] == 3


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
    """桶键只取 `where` 空格前的部分：真实日志里战斗内的 where 是
    `combat T1`、`combat T3`……按回合各成一桶，会把「战斗内停手」这一件事
    打散成一堆一次性的小桶（实测：combat T1:5, T3:4, T5:3, T2:3, T6:2,
    T4:1, T9:1），而 `NRewardsScreen` 这类界面桶却整桶聚在一起、显得比战斗
    更「值得注意」。切掉回合号，让 `combat T4` 聚进 `combat` 一桶，才能与
    界面桶站在同一个粒度上比较。"""
    combat = state(in_combat=True, combat={"turn": 4})
    journal.record(combat, "auto_combat 停手", "血量过低", by="heuristic", kind="stop")
    journal.record(state(screen={"type": "NMerchantRoom"}), "auto_run 停手", "商店要挑",
                   by="heuristic", kind="stop")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    b = journal.brief()
    assert b["stops"] == {"combat": 1, "NMerchantRoom": 1}
    assert "血量过低" in b["stop_reasons"]


def test_停手理由只留最后三条且顺序不是最早三条():
    combat = state(in_combat=True, combat={"turn": 1})
    journal.record(combat, "auto_combat 停手", "理由一", by="heuristic", kind="stop")
    journal.record(combat, "auto_combat 停手", "理由二", by="heuristic", kind="stop")
    journal.record(combat, "auto_combat 停手", "理由三", by="heuristic", kind="stop")
    journal.record(combat, "auto_combat 停手", "理由四", by="heuristic", kind="stop")
    journal.record(combat, "auto_combat 停手", "理由五", by="heuristic", kind="stop")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    b = journal.brief()
    assert b["stop_reasons"] == ["理由三", "理由四", "理由五"]


def test_brief只收模型亲自做的构筑决策():
    journal.record(state(screen={"type": "NRewardsScreen"}), "pick 剑柄打击", "补输出")
    journal.record(state(in_combat=True, combat={"turn": 2}), "play_card 打击", "常规",
                   by="heuristic")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    b = journal.brief()
    assert [x["action"] for x in b["builds"]] == ["pick 剑柄打击"]


def test_builds不混入终局记录():
    """`_is_build()` 原先只排除了 `kind=="plan"`，没排 `run_end`——于是
    `brief().builds` 里会混进一条「本局结束于第 12 层」，而 floor / ended
    两个字段已经把这件事说过一遍，混进 builds 纯属重复。"""
    journal.record(state(screen={"type": "NRewardsScreen"}), "pick 剑柄打击", "补输出")
    # by="heuristic"：只让这条记录顺带触发 observe() 补一条 run_end，
    # 自己不作为 build 混进来（那是既有的 by != heuristic 过滤已经保证的事，
    # 不是本用例要钉的东西）。
    journal.record(state(floor=12, hp=0, game_over=True), "auto_combat 停手", "本局已结束",
                   by="heuristic")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    b = journal.brief()
    actions = [x["action"] for x in b["builds"]]
    assert actions == ["pick 剑柄打击"]
    assert not any("本局结束" in a for a in actions)


def test_brief带出上一局写下的计划():
    journal.record_plan(state(floor=1), "这一局优先拿降费件")
    journal.record(state(floor=5), "往下打", "")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    assert journal.brief()["plan"] == "这一局优先拿降费件"


def test_一局内重新规划时取最新那条():
    """重新规划是带着更多信息做的，它比开局时的猜想更值得传给下一局。"""
    journal.record_plan(state(floor=1), "开局：优先拿降费件")
    journal.record_plan(state(floor=9), "改主意：先补格挡，不然撑不到 Boss")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    assert journal.brief()["plan"] == "改主意：先补格挡，不然撑不到 Boss"


def test_上一局没有终局记录时降级为见过的最大层数():
    journal.record(state(floor=3), "a", "")
    journal.record(state(floor=8), "b", "")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    b = journal.brief()
    assert b["ended"] is False and b["floor"] == 8


def test_未结束的局act降级为最后一条有act的记录():
    """`floor` / `hp` 都有降级取值（最大层数 / 最后一条），此前只有 `act`
    只从 `run_end` 那条取——于是未结束的局 `act` 恒为 null，而 get_brief
    的说明书承诺的是「结束在第几层第几幕」。补上与 hp 同样的降级。"""
    journal.record(state(floor=3), "a", "")
    journal.record(state(floor=8), "b", "")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    b = journal.brief()
    assert b["ended"] is False
    assert b["act"] == 1      # state() 里 run.act 恒为 1


def test_runs_back小于1时按无上一局处理():
    """`runs_back <= 0` 曾经会静默返回错的局（实测：0 → 最老那局，
    -1 → 最新那局）。非法输入该按「没有上一局」处理，而不是悄悄给出
    一个语义不对的结果。"""
    journal.record(state(floor=5, character="Ironclad"), "a", "")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    assert journal.brief(runs_back=0)["run"] is None
    assert journal.brief(runs_back=-1)["run"] is None


def test_brief读不到日志也不抛(monkeypatch):
    monkeypatch.setattr(journal, "PATH", "Z:/根本不存在的盘/decisions.jsonl")
    assert journal.brief()["run"] is None


# --------------------------------------------------------------------------
#  has_plan：失败即放行（不能拿 _read_all 的空列表当「读到了」的证据）
# --------------------------------------------------------------------------


def test_has_plan_本局有plan条目():
    journal.record_plan(state(floor=1), "开局计划：优先拿降费件")
    assert journal.has_plan(state(floor=1)) is True


def test_has_plan_日志存在但本局没有plan条目():
    journal.record(state(floor=1), "随便一步", "")   # 让日志文件确实存在
    assert journal.has_plan(state(floor=1)) is False


def test_has_plan_日志根本读不到就放行(monkeypatch):
    """修复前 `_read_all()` 把「读不到」也吞成空列表，`any(...)` 直接是
    False，外层 `except Exception: return True` 永远轮不到 —— 这条用来
    钉死「失败方向是放行」这句承诺是真的兑现了。"""
    monkeypatch.setattr(journal, "PATH", "Z:/根本不存在的盘/decisions.jsonl")
    monkeypatch.setattr(journal, "_RUN_FILE", "Z:/根本不存在的盘/current-run.json")
    assert journal.has_plan(state(floor=1)) is True


# --------------------------------------------------------------------------
#  「失败即放行」只修了读的一半：日志可读、却写不进去（Important 1）
#
#  修复前：`record_plan` 写失败时被 `_warn` 静默吞掉，`has_plan` 读得到
#  日志、只是查不到 plan 条目，恒为 False；而 `set_plan` 无条件回报
#  `ok: True`。于是模型以为计划记下来了，下一次 auto_run 又在第 1 层
#  卡住——写失败换了个入口，复现同一种死循环。
# --------------------------------------------------------------------------


def test_record写成功时返回True():
    assert journal.record(state(), "pick 某张牌", "理由") is True


def test_record写不进去时返回False但依然不抛异常(monkeypatch):
    """`test_写不进去也不抛异常` 钉的是「不抛」，这条额外钉「如实返回
    False」——两条并不重复：前者是安全底线，后者是给上层（`set_plan`）
    做判断用的信号。"""
    _block_append(monkeypatch)
    assert journal.record(state(), "pick 某张牌", "理由") is False


def test_record_plan写不进去时返回False(monkeypatch):
    _block_append(monkeypatch)
    assert journal.record_plan(state(floor=1), "开局计划") is False


def test_日志可读却写不进去时has_plan仍然放行(monkeypatch):
    """区别于「读不到」：这里日志文件本身可读、也确实有内容（只是没有
    plan 条目），只是新的写入会失败。修复前 `has_plan` 只处理了读失败，
    这种情况下 `any(...)` 会诚实地算出 False，永远不满足，`auto_run`
    就会在第 1 层无限期停手。"""
    # 先在写还没坏的时候，让日志确实存在且可读（但没有 plan 条目）
    journal.record(state(floor=1), "已有一条普通记录", "")
    assert journal._log_unreadable() is False      # 此刻确实读得到

    _block_append(monkeypatch)
    assert journal._log_unwritable() is True
    assert journal.has_plan(state(floor=1)) is True   # 放行，不再无限期等待


# --------------------------------------------------------------------------
#  两个此前零覆盖的分支（Important 3）
# --------------------------------------------------------------------------


def test_log_unreadable_文件存在但打不开():
    """`_log_unreadable` 的 `except OSError: return True` 分支此前零覆盖——
    唯一的放行用例走的是「盘符不存在」，命中的是 `isdir` 那一支。这里用
    「PATH 本身其实是个目录」来稳定触发「文件存在但打不开」，跨平台都会
    在 `open()` 时报 `OSError`（IsADirectoryError / PermissionError 皆是
    其子类），不必依赖 chmod 在部分平台/账户下不生效的坑。"""
    os.makedirs(journal.PATH)      # PATH 自己就是个目录，open() 必然报错
    assert journal._log_unreadable() is True


def test_全新安装时logs目录不存在第一层仍然停手(tmp_path, monkeypatch):
    """spec.md §4.4：`logs/` 目录整个都不存在（全新安装、一次都没
    `record()` 过）时，第 1 层仍要求先复盘（`has_plan` 返回 False，不是
    放行）。这份正确性现在由 `_log_unreadable`/`_log_unwritable` 共用的
    `_ensure_dir()` 自己保证（探测前先试着建目录，建得出来就不算真故障）——
    不再依赖 `has_plan()` 里 `run_id(state)` 与探针的调用顺序（`run_id`
    内部的 `_save_current()` 恰好也会建目录，那份副作用此前是本测试能通过
    的唯一原因）。"""
    fresh = tmp_path / "brand-new" / "decisions.jsonl"
    fresh_run = tmp_path / "brand-new" / "current-run.json"
    monkeypatch.setattr(journal, "PATH", str(fresh))
    monkeypatch.setattr(journal, "_RUN_FILE", str(fresh_run))
    assert not os.path.isdir(os.path.dirname(fresh))       # 目录确实还不存在

    assert journal.has_plan(state(floor=1)) is False       # 仍要求复盘，不是放行
