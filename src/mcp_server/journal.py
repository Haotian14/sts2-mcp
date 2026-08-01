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


def _append(entry: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with open(PATH, "a", encoding="utf-8") as f:
            # 上一行没写完就被杀进程（调试期天天在杀游戏和重启 server），
            # 追加会直接接在那半行后面，把**下一条**也一起毁掉。
            # 补一个换行，让损坏止于那一行。
            if _truncated():
                f.write("\n")
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        _warn(exc)


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
           kind: str = "") -> None:
    """记一步决策。

    `state` 是做这个决策时的局面。动作工具只拿得到**执行后**的状态，
    直接用即可 —— 层数、金币、血量在一次点击前后基本不变；而战斗内的
    `auto_combat` 拿得到出牌前的状态，那边传的是前者，更准。

    `by` 区分是谁做的决定：`heuristic` 是本地启发式，`model` 是模型自己。
    跨局复盘时这一栏最有用 —— 死于启发式的盲区，和死于模型的误判，
    是两种完全不同的问题（spec §6.2 那次全灭是前者）。

    `kind` 标出「这条不是常规操作」（如 `stop`：启发式停手交还）。摘要会按它
    保留 —— 停手的理由标出了启发式的边界在哪，恰恰是最该留下来的一条，
    而它偏偏也是 `by=heuristic` 的战斗内记录，不特别标一下就会被筛掉。
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
        _append({k: v for k, v in entry.items() if v not in (None, "")})

        if _mark_ended(state):
            _append({
                "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "run": entry["run"],
                "kind": "run_end",
                "floor": run.get("total_floor"),
                "act": run.get("act"),
                "hp": player.get("hp"),
                "why": "本局结束（game_over）",
            })
    except Exception as exc:      # noqa: BLE001 —— 日志绝不能弄坏一步棋
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


def digest(runs: int = 3, per_run: int = 40) -> dict[str, Any]:
    """把日志压成能塞进上下文的一份摘要。

    **只留高价值决策**：战斗内一步一张牌的记录量太大且价值低（那是启发式的
    活儿），而卡牌三选一、商店、路线这些决定一局上限的决策（strategy.md §5）
    必须留全。故战斗内只保留模型亲自出手的那些 —— 那正是启发式停手交还的拐点。
    """
    entries = _read_all()
    order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        rid = e.get("run") or "?"
        if rid not in grouped:
            grouped[rid] = []
            order.append(rid)
        grouped[rid].append(e)

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
