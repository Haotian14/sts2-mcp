"""决策日志（spec.md 的 6.5）—— 把「当时的局面 / 做了什么 / 为什么」落到盘上。

【为什么这是「唯一能让它变强的东西」】
一局打完，所有的判断都随上下文一起蒸发了。下一局重开时，模型既不知道上一局
死在哪，也不知道上一局的卡牌三选一到底选了什么、后来有没有用上。
strategy.md 里那几条实战结论之所以立得住，全靠人工把当时的局面抄了下来 ——
这个模块就是把那件事自动化。

【形状：一行一条 JSON，追加写，永不重写】
`logs/decisions.jsonl`。选 JSONL 而不是每局一个文件，是因为**跨局比对才是重点**：
「上一局在第 7 层选了什么牌」这种问题，单文件一次扫描即得，不必先列目录。
标识一律用类型短名（`StrikeIronclad`），与 `/state` 同源，才能跨局做字典键。

【局的边界怎么定】
`/state` 里没有种子一类的局标识（查过 `RunState`，没有导出）。故由本模块自己
维护 `logs/current-run.json`：角色或天梯层数变了、总层数**倒退**了、或上一局
已记过结束 —— 三者任一即判为新的一局。它落在盘上而不是内存里，因为 MCP
server 会重启，而一局要打几个小时。

【绝不因为记日志而弄坏一步棋】
所有落盘操作都吞掉异常：日志是旁路，游戏才是主线。写不进去顶多丢一条记录，
但绝不能让一次磁盘错误把已经下发的动作变成一个失败回报。
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

PATH = os.environ.get("STS2MCP_JOURNAL") or os.path.join(_ROOT, "logs", "decisions.jsonl")

# 局的身份要跨进程存活 —— MCP server 重启不该把一局劈成两半
_RUN_FILE = os.path.join(os.path.dirname(PATH), "current-run.json")

_warned = False


def _warn(exc: BaseException) -> None:
    """日志坏了要说一声，但只说一次 —— 一局几百个动作，刷屏比不写更糟。"""
    global _warned
    if not _warned:
        _warned = True
        print(f"[journal] 决策日志写入失败，已放弃记录：{exc}", file=sys.stderr)


# --------------------------------------------------------------------------
#  局的身份
# --------------------------------------------------------------------------


def _marker(state: dict[str, Any]) -> dict[str, Any]:
    run = state.get("run") or {}
    player = state.get("player") or {}
    return {
        "character": player.get("character"),
        "ascension": run.get("ascension"),
        "total_floor": run.get("total_floor"),
    }


def _load_current() -> dict[str, Any] | None:
    try:
        with open(_RUN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_current(cur: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(_RUN_FILE), exist_ok=True)
        with open(_RUN_FILE, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False)
    except OSError as exc:
        _warn(exc)


def _is_same_run(cur: dict[str, Any], mark: dict[str, Any], game_over: bool) -> bool:
    if cur.get("character") != mark["character"] or cur.get("ascension") != mark["ascension"]:
        return False
    old, new = cur.get("total_floor"), mark["total_floor"]
    # 总层数只增不减。倒退即为重开了一局。
    # 读不到层数时不据此判新局 —— 主菜单、游戏结束界面都读不到，
    # 那不代表换了一局（换局要靠角色/天梯变化或已记过结束来判）。
    if isinstance(old, int) and isinstance(new, int) and new < old:
        return False
    # 已记过结束的一局：只要局面还停在 game_over 上，那就还是它（死亡界面上
    # 还要点几下才出得去，那几下属于上一局）。等到再看见一个**没有** game_over
    # 的局面，才说明新的一局开始了。
    if cur.get("ended") and not game_over:
        return False
    return True


def run_id(state: dict[str, Any]) -> str:
    """当前这一局的标识，形如 `20260801-124500-Ironclad`。

    每次调用都会顺手把「见过的最大层数」记下来，供下次判断是否倒退。
    """
    # 不在局中（主菜单、角色选择、死亡后往外走的那几下）不属于任何一局。
    # 早先没分开，于是每次开新局前的几次点击都会凭空造出一个
    # `…-unknown` 的「局」，混在真实战绩里。它们归进一个固定的 menu 桶。
    if not state.get("in_run"):
        return "menu"

    mark = _marker(state)
    game_over = bool((state.get("run") or {}).get("game_over"))
    cur = _load_current()

    if cur and _is_same_run(cur, mark, game_over):
        if isinstance(mark["total_floor"], int) and mark["total_floor"] != cur.get("total_floor"):
            cur["total_floor"] = mark["total_floor"]
            _save_current(cur)
        return str(cur.get("id"))

    # 秒级时间戳不足以区分两局 —— 死了立刻重开，两条记录同属一秒，
    # 标识撞在一起就等于把两局混成一份（实测：单测里四局全落在同一秒）。
    # 与上一局撞号时往后加序号。
    ident = f"{time.strftime('%Y%m%d-%H%M%S')}-{mark['character'] or 'unknown'}"
    previous = str((cur or {}).get("id") or "")
    if previous.split("#")[0] == ident:
        ident = f"{ident}#{int(previous.split('#')[1]) + 1 if '#' in previous else 2}"

    cur = {"id": ident, "ended": False, **mark}
    _save_current(cur)
    return str(cur["id"])


def _mark_ended(state: dict[str, Any]) -> bool:
    """本局是否刚刚结束（只在第一次看到 game_over 时返回 True）。"""
    if not (state.get("run") or {}).get("game_over"):
        return False
    cur = _load_current()
    if not cur or cur.get("ended"):
        return False
    cur["ended"] = True
    _save_current(cur)
    return True


# --------------------------------------------------------------------------
#  写
# --------------------------------------------------------------------------


def option_name(options: Any, i: int) -> str:
    """把「下标」还原成「名字」。

    日志里记 `pick(2)` 等于没记 —— 下标当场就过期，三天后回头看无从还原。
    故动作名一律在**下发之前**从局面里取出来（`闪电霹雳` / `Monster` /
    `无情猛攻（38金）`）。放在这里是因为 server 与 autorun 两条路都要用。
    """
    opt = next((o for o in options or [] if o.get("i") == i), None)
    if not opt:
        return f"#{i}"
    name = opt.get("title") or opt.get("id") or opt.get("type") or f"#{i}"

    # `cost` 在两处含义**完全不同**：商店选项上是金价，待答选牌的选项上是
    # 能量费用。实测把后者写成了「痛击+（2金）」—— 日志里一句假话，
    # 而日志的全部价值就在于事后能信它。
    # 判据：待答选牌的选项带卡牌 `type`（Attack/Skill…），商店选项没有。
    cost = opt.get("cost")
    if isinstance(cost, int) and "type" not in opt:
        return f"{name}（{cost}金）"
    return str(name)


def _where(state: dict[str, Any]) -> str:
    """这一步发生在哪 —— 战斗第几回合 / 什么界面 / 地图上。"""
    if state.get("in_combat"):
        turn = (state.get("combat") or {}).get("turn")
        return f"combat T{turn}" if turn is not None else "combat"
    screen = state.get("screen") or {}
    if screen.get("type"):
        return str(screen["type"])
    if (state.get("map") or {}).get("can_move"):
        return "map"
    if not state.get("in_run"):
        return "menu"
    return (state.get("run") or {}).get("room") or "?"


def _append(entry: dict[str, Any]) -> bool:
    """追加一行。返回是否真的落了盘 —— 调用方（`record`）要把这件事如实
    传出去，`_warn` 只管打日志、不够用来判断有没有写成功。"""
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with open(PATH, "a", encoding="utf-8") as f:
            # 上一行没写完就被杀进程（调试期天天在杀游戏和重启 server），
            # 追加会直接接在那半行后面，把**下一条**也一起毁掉。
            # 补一个换行，让损坏止于那一行。
            if _truncated():
                f.write("\n")
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        _warn(exc)
        return False


def _truncated() -> bool:
    """文件非空且最后一个字节不是换行 —— 说明上一次写到一半断了。"""
    try:
        with open(PATH, "rb") as f:
            if f.seek(0, os.SEEK_END) == 0:
                return False
            f.seek(-1, os.SEEK_END)
            return f.read(1) != b"\n"
    except OSError:
        return False


def record(state: dict[str, Any], action: str, why: str = "", by: str = "model",
           kind: str = "") -> bool:
    """记一步决策。返回是否真的落了盘（写失败时为 `False`，但绝不抛异常）。

    `state` 是做这个决策时的局面。动作工具只拿得到**执行后**的状态，
    直接用即可 —— 层数、金币、血量在一次点击前后基本不变；而战斗内的
    `auto_combat` 拿得到出牌前的状态，那边传的是前者，更准。

    `by` 区分是谁做的决定：`heuristic` 是本地启发式，`model` 是模型自己。
    跨局复盘时这一栏最有用 —— 死于启发式的盲区，和死于模型的误判，
    是两种完全不同的问题（spec §6.2 那次全灭是前者）。

    `kind` 标出「这条不是常规操作」（如 `stop`：启发式停手交还）。摘要会按它
    保留 —— 停手的理由标出了启发式的边界在哪，恰恰是最该留下来的一条，
    而它偏偏也是 `by=heuristic` 的战斗内记录，不特别标一下就会被筛掉。

    ⚠️ 返回值只是**如实相告**，不改变「绝不因为写失败而抛异常」这条铁律——
    调用方（如 `record_plan` → `set_plan`）可以选择看这个返回值，也可以
    像大多数调用点一样完全不看；日志始终是旁路，游戏永远是主线。
    """
    try:
        state = state or {}
        run = state.get("run") or {}
        player = state.get("player") or {}
        entry = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "run": run_id(state),
            "act": run.get("act"),
            "floor": run.get("total_floor"),
            "hp": player.get("hp"),
            "max_hp": player.get("max_hp"),
            "gold": run.get("gold"),
            "where": _where(state),
            "by": by,
            "kind": kind,
            "action": action,
            "why": why,
        }
        ok = _append({k: v for k, v in entry.items() if v not in (None, "")})
        observe(state)
        return ok
    except Exception as exc:      # noqa: BLE001 —— 日志绝不能弄坏一步棋
        _warn(exc)
        return False


def observe(state: dict[str, Any]) -> None:
    """看一眼局面，只为捕捉「本局结束」这一件事，不记决策。

    死亡是**动作之后**才成立的，而决策记的是动作**之前**的局面 —— 于是
    一整局打完，日志里可能一条终局记录都没有。2026-08-01 实测：Boss 战最后
    一次 end_turn 记的是出手前（`game_over` 仍是 false）的局面，人死了，
    日志却显示这局还在进行中。故动作做完要拿**执行后**的状态再看一眼。
    """
    try:
        if not state or not _mark_ended(state):
            return
        run, player = state.get("run") or {}, state.get("player") or {}
        _append({
            "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "run": run_id(state),
            "kind": "run_end",
            "floor": run.get("total_floor"),
            "act": run.get("act"),
            "hp": player.get("hp"),
            # 终局记录也带 action：摘要的每一条都有这一栏，缺了读的人就得
            # 为这一种记录写特例
            "action": f"本局结束于第 {run.get('total_floor')} 层",
            "why": "本局结束（game_over）",
        })
    except Exception as exc:      # noqa: BLE001
        _warn(exc)


# --------------------------------------------------------------------------
#  读
# --------------------------------------------------------------------------


def _read_all() -> list[dict[str, Any]]:
    try:
        with open(PATH, encoding="utf-8") as f:
            out = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue        # 半行（写到一半被杀进程）不该毁掉整份日志
            return out
    except OSError:
        return []


def _group_by_run(
    entries: list[dict[str, Any]], *, drop_menu: bool = False,
) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    """按局号分组，`order` 记局号首次出现的顺序（分组规则的唯一出处）。

    `digest` 与 `brief` 都要按局号分组，且分组规则必须是同一份 —— 局号
    判定一旦以后要改，两处各改一遍最容易漏掉一处，日志就会在两个读口上
    读出不一样的「局」。

    `drop_menu` 只供 `brief` 用：主菜单上的点击不属于任何一局，不该被当成
    「上一局」。`digest` 面向全部历史且不受这条规则约束，故默认不过滤，
    以保持其既有对外行为不变。
    """
    order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        rid = str(e.get("run") or "?")
        if drop_menu and rid == "menu":
            continue
        if rid not in grouped:
            grouped[rid] = []
            order.append(rid)
        grouped[rid].append(e)
    return order, grouped


def digest(runs: int = 3, per_run: int = 40) -> dict[str, Any]:
    """把日志压成能塞进上下文的一份摘要。

    **只留高价值决策**：战斗内一步一张牌的记录量太大且价值低（那是启发式的
    活儿），而卡牌三选一、商店、路线这些决定一局上限的决策（strategy.md §3）
    必须留全。故战斗内只保留模型亲自出手的那些 —— 那正是启发式停手交还的拐点。
    """
    entries = _read_all()
    order, grouped = _group_by_run(entries)

    out = []
    for rid in order[-runs:] if runs > 0 else order:
        items = grouped[rid]
        floors = [e["floor"] for e in items if isinstance(e.get("floor"), int)]
        ended = any(e.get("kind") == "run_end" for e in items)
        key = [
            e for e in items
            if e.get("kind")                                      # 停手、终局：一律留
            or e.get("by") != "heuristic"                         # 模型亲自出手的拐点
            or not str(e.get("where", "")).startswith("combat")   # 界面上的构筑决策
        ]
        out.append({
            "run": rid,
            "floors": max(floors) if floors else None,
            "ended": ended,
            "steps": len(items),
            "decisions": [
                {k: v for k, v in e.items()
                 if k in ("floor", "hp", "where", "by", "kind", "action", "why")}
                for e in key[-per_run:]
            ],
        })
    return {"path": PATH, "runs": out}


def record_plan(state: dict[str, Any], plan: str) -> bool:
    """记下本局的开局计划（spec.md 6.3b）。返回是否真的落了盘。

    形状上就是一条普通决策，只是 `kind="plan"`。`digest` 的筛选规则是
    「带 kind 的一律留」，所以它天然会出现在下一局的 `brief()` 里 ——
    闭环不需要额外代码。

    ⚠️ `set_plan` 工具靠这个返回值判断能不能对模型说「记下来了」——
    写失败时 `has_plan` 会转为放行（见其文档），但那只解决了「不再卡死」，
    不能让 `set_plan` 平白说谎，模型有权知道这一条计划其实没能落盘。
    """
    return record(state, "本局计划", plan, by="model", kind="plan")


def _ensure_dir() -> bool:
    """确保日志目录存在，返回目录**现在**是不是在。

    `_log_unreadable` 与 `_log_unwritable` 都要判断「目录摸不到」是真故障
    还是「全新安装、还没来得及建」——而这两者单看一次 `os.path.isdir`
    分不出来，非得先试着建一次不可（建得出来就是后者，建不出来才是前者）。
    这一步是两个探针共用的唯一交集，抽到这里，谁也不必再依赖对方或
    `has_plan` 里别的调用是否恰好先把目录建了出来。

    ⚠️ 与 `_log_unreadable`/`_log_unwritable` 判的是两件不同的事（读坏了 /
    写坏了）不同，这里只做「目录在不在」这一件事——不要把它也理解成
    第三个探针，它只是两者共用的前置步骤。
    """
    d = os.path.dirname(PATH) or "."
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return os.path.isdir(d)


def _log_unreadable() -> bool:
    """日志是不是**真的**读不到 —— 区别于「还没写过」。

    `_read_all()` 把「文件不存在」和「文件坏了/权限不对」一律归成空列表 ——
    这对它自己的用途（摘要、brief）没问题，半局没记录就该显示成空。但
    `has_plan` 要的是另一件事：只有「真故障」才该按「失败即放行」处理，
    「日志是空的 / 还没轮到本局写过 plan」跟平常没查到条目一样，走正常
    判定即可（否则全新环境第一次开局就会永远卡在第 1 层 —— 见本模块历史
    bug：曾经就是直接拿 `_read_all()` 的返回值当「读没读到」的证据，
    结果两种情况在这里被混成了一种，「失败即放行」名不副实）。

    判法：目录**建不出来**，才是真故障（磁盘/路径问题）；目录建得出来但
    文件还没创建，是「空日志」，不算故障；目录、文件都在但打不开
    （权限、损坏），才是「读不到」。

    目录这一步交给 `_ensure_dir()`：它会先试着建一次目录，建不出来才算
    数。这样「全新安装、logs/ 目录从未创建过」与「目录真的建不出来」不再
    长得一模一样——不必依赖 `has_plan` 里别的调用（如 `run_id`）恰好先把
    目录建了出来，本函数单独调用也是对的，调用顺序也不再重要。
    """
    if not _ensure_dir():
        return True
    try:
        with open(PATH, encoding="utf-8"):
            return False
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _log_unwritable() -> bool:
    """日志**读得到**，但写不进去 —— 磁盘满、文件只读、ACL 限制等。

    这是 `_log_unreadable` 之外的另一半：读和写走的是不同的系统调用，
    一份日志完全可以「读得到、写不进」（最常见的例子就是文件被设成只读）。
    `record`/`record_plan` 写失败时早已如实返回 `False`，但 `has_plan`
    在没有人恰好触发过一次写入的情况下也得能独立判断出「写会失败」，
    不然它会在这条计划注定存不下来的前提下，一直等一个永远不会兑现的写入。

    探测方式与 `_append` 同路：目录这一步交给 `_ensure_dir()`（建不出来
    即真故障），再以追加模式打开一次，打开成功就立刻关闭，不写入任何
    字节。⚠️ 但探测本身**并非无副作用**：目录不存在时会被 `_ensure_dir`
    建出来；文件不存在时 `open(PATH, "a")` 也会顺手建出一个 0 字节的空
    文件——这与 `_append` 真正落盘时「建目录」这一步一致、无害，但绝不是
    「什么都不留下」，别再让注释说反话。
    """
    if not _ensure_dir():
        return True
    try:
        with open(PATH, "a", encoding="utf-8"):
            return False
    except OSError:
        return True


def has_plan(state: dict[str, Any]) -> bool:
    """本局是否已经写过开局计划。

    ⚠️ **失败方向是「放行」**：读不到日志、日志坏了、日志读得到但写不
    进去，一律返回 True。这条判据只用来决定 runner 要不要停一次手，而
    日志是旁路 —— 让一次磁盘错误把整局卡在第 1 层，比漏做一次复盘糟得多。

    这曾经只修了一半：`_log_unreadable` 只管「读」这一侧，而「日志可读、
    却写不进 `record_plan`」是另一个独立的故障模式（文件只读、ACL 限制、
    磁盘满）——那种情况下 `_read_all()` 能正常读出「本局没记过 plan」，
    `any(...)` 于是恒为 False，`set_plan` 却因为 `record_plan` 内部把
    `OSError` 吞掉而无条件回报成功，模型被告知「记下来了」，下一次
    `auto_run` 又卡在同一个停手点——写失败换了个入口，复现同一种死循环。
    `_log_unwritable()` 补的就是这一半。
    """
    try:
        rid = run_id(state)
        if rid == "menu":
            return True
        if _log_unreadable():
            return True
        if _log_unwritable():
            return True
        return any(e.get("run") == rid and e.get("kind") == "plan" for e in _read_all())
    except Exception:      # noqa: BLE001 —— 日志绝不能弄坏一步棋
        return True


# 上一局的「构筑决策」：模型亲自做的、且不在战斗内的那些。
# 判据与 digest 同源（by != heuristic），因为二者要的是同一批东西：
# 决定一局上限的地方（strategy.md §3）。
# `plan` 与 `run_end` 都要排除在外：前者是开局计划，单列在 `plan` 字段里；
# 后者是「本局结束于第 N 层」这句话——floor / ended 两个字段已经说过一遍，
# 混进 builds 里就是同一件事被复述了两遍。
def _is_build(entry: dict[str, Any]) -> bool:
    return (entry.get("by") != "heuristic"
            and not str(entry.get("where", "")).startswith("combat")
            and entry.get("kind") not in ("plan", "run_end"))


def brief(runs_back: int = 1) -> dict[str, Any]:
    """上一局的硬事实。**不生成任何建议**（spec.md 6.3b）。

    「所以这一局该改什么」由模型自己写，再经 `record_plan` 记回日志 ——
    与 6.5 的结论一致：理由得由做决定的人写，模板拼出来的建议只会是套话。
    """
    empty: dict[str, Any] = {
        "run": None, "character": None, "ended": False, "floor": None,
        "act": None, "hp": None, "stops": {}, "stop_reasons": [],
        "builds": [], "plan": None,
    }
    try:
        if runs_back < 1:      # 非法输入（0、负数）一律当「没有上一局」，不静默返回错的局
            return empty
        entries = _read_all()
        # 只有「尚未结束」的那一局才算「当前局」要被排除——一局结束后、新局
        # 还没开始前，current-run.json 记的正是**刚打完的那一局**（ended 为
        # true）。若不分这一层，死亡界面/主菜单上调 brief() 会把刚死的这局
        # 也一并当成「当前局」排除掉，结果错把上上局的事实当成「上一局」
        # 返回（Important 1）。
        cur = _load_current() or {}
        current = str(cur.get("id") or "") if not cur.get("ended") else ""
        order, grouped = _group_by_run(entries, drop_menu=True)

        previous = [rid for rid in order if rid != current]
        if not previous or runs_back > len(previous):
            return empty
        rid = previous[-runs_back]
        items = grouped[rid]

        end = next((e for e in items if e.get("kind") == "run_end"), None)
        floors = [e["floor"] for e in items if isinstance(e.get("floor"), int)]
        hps = [e["hp"] for e in items if isinstance(e.get("hp"), int)]
        acts = [e["act"] for e in items if isinstance(e.get("act"), int)]

        stops: dict[str, int] = {}
        reasons: list[str] = []
        for e in items:
            if e.get("kind") != "stop":
                continue
            # 桶键只取空格前的部分：战斗内的 where 形如 `combat T1`、`combat T3`……
            # 按回合各成一桶，会把「战斗内停手」这一件事打散成一堆一次性的小桶
            # （实测：combat T1:5, T3:4, T5:3, T2:3, T6:2, T4:1, T9:1），而
            # `NRewardsScreen` 这类界面桶却整个聚在一起。切掉回合号，让战斗内
            # 的停手聚成 `combat` 一桶，与界面桶站在同一个粒度上比较。
            where = str(e.get("where") or "?").split(" ")[0]
            stops[where] = stops.get(where, 0) + 1
            if e.get("why"):
                reasons.append(str(e["why"]))

        # 取最后一条而非第一条：一局内可能重新规划（例如打到第二幕才看清
        # 牌组短板），后写的那条是带着更多信息做的，比开局时的猜想更值得
        # 传给下一局。
        plan = next(
            (str(e.get("why") or "") for e in reversed(items) if e.get("kind") == "plan"),
            None,
        )

        return {
            "run": rid,
            # 角色名只存在于局标识里（`20260801-124500-Ironclad`），日志条目
            # 本身不记角色。这是本模块自己生成的格式，故可以拆；`#2` 后缀是
            # 同秒撞号时加的，要先去掉。
            "character": rid.split("#")[0].split("-")[-1] or None,
            "ended": end is not None,
            "floor": (end or {}).get("floor") or (max(floors) if floors else None),
            # 与 floor / hp 同一套降级：没有终局记录时（runner 中途被打断、
            # 或死亡时机的记录没能补上），取本局最后一条带 act 的记录 ——
            # 否则未结束的局 act 恒为 null，而 get_brief 的说明书承诺的是
            # 「结束在第几层第几幕」。
            "act": (end or {}).get("act") if end else (acts[-1] if acts else None),
            "hp": (end or {}).get("hp") if end else (hps[-1] if hps else None),
            "stops": stops,
            "stop_reasons": reasons[-3:],
            "builds": [
                {"floor": e.get("floor"), "action": e.get("action"), "why": e.get("why", "")}
                for e in items if _is_build(e)
            ],
            "plan": plan,
        }
    except Exception:      # noqa: BLE001 —— 复盘坏了不该影响下一步棋
        return empty
