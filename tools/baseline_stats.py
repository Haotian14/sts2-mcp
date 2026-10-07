"""统计一组对局日志（logs/<前缀>-N.log）：死亡层数、死因、掉血、精英与金币。

用法：
    python tools/baseline_stats.py baseline          # 统计 logs/baseline-*.log
    python tools/baseline_stats.py exp1 baseline     # 两组对比

跑一组局的方式（每局一个日志，便于按局拆分）：
    for n in $(seq 1 10); do python scripts/play.py --runs 1 --new-run silent > logs/exp1-$n.log 2>&1; done

单局方差很大（基线 9 局标准差约 5 层），两组平均差 4～5 层以上才比较可信。
「到达」行（runner._track，2026-10-07 起）给出每层的准确血量；更早的日志没有，相关列留空。
"""

from __future__ import annotations

import glob
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict

LOGS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
ROOM_ZH = {"怪": "普通怪", "精英": "精英", "Boss": "Boss", "?": "问号房", "火": "休息点",
           "店": "商店", "箱": "宝箱", "古": "古神"}
EN = {"Monster": "怪", "Elite": "精英", "Boss": "Boss", "RestSite": "火", "Shop": "店",
      "Treasure": "箱", "Unknown": "?", "Ancient": "古"}
COMPLETED = "已打完 1 局"


def _read(path: str) -> str:
    raw = open(path, "rb").read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def parse(path: str) -> dict | None:
    txt = _read(path)
    m = re.search(r'\{\s*"stopped".*\}\s*$', txt, re.S)
    if not m:
        return None
    summary = json.loads(m.group(0))
    lines = [l for l in txt.splitlines() if l.startswith("[")]

    room_at: dict[int, str] = {}          # 第 N 层是什么房间（由上一层的移动决定）
    for l in lines:
        mv = re.match(r"\[(\d+)\] move \d+ — (?:路线 (\S+?)→|路线 (\S+?)（|只有一条路（(\w+)）)", l)
        if mv:
            room_at[int(mv.group(1)) + 1] = mv.group(2) or mv.group(3) or EN.get(mv.group(4), mv.group(4))

    arrive: dict[int, int] = {}           # 到达第 N 层时的血量
    for l in lines:
        am = re.match(r"\[(\d+)\] 到达 \S* — 血 (\d+)/\d+", l)
        if am:
            arrive.setdefault(int(am.group(1)), int(am.group(2)))

    floor = summary.get("floor") or 0
    foes = Counter()
    for l in lines:
        if l.startswith(f"[{floor}] play_card"):
            foes.update(re.findall(r"→([A-Za-z]+)", l.split("规划", 1)[-1]))

    # 每场战斗掉血 = 到达本层血量 − 到达下一层血量（死亡那层：到达血量全掉）
    losses: list[tuple[str, int]] = []
    for f, hp in arrive.items():
        room = ROOM_ZH.get(room_at.get(f, ""), "")
        if room not in ("普通怪", "精英", "Boss"):
            continue
        after = arrive.get(f + 1, 0 if f == floor else None)
        if after is not None:
            losses.append((room, hp - after))

    return {
        "log": os.path.basename(path), "stopped": summary.get("stopped"),
        "floor": floor, "act": summary.get("act"),
        "room": ROOM_ZH.get(room_at.get(floor, "?"), room_at.get(floor, "?")),
        "foe": foes.most_common(1)[0][0] if foes else "?",
        "hp_in": arrive.get(floor),
        "elites": sum(1 for f in room_at.values() if f == "精英"),
        "gold": summary.get("gold"), "deck_size": len(summary.get("deck") or []),
        "losses": losses,
    }


def load(prefix: str) -> list[dict]:
    paths = glob.glob(os.path.join(LOGS, f"{prefix}-*.log"))
    paths.sort(key=lambda p: int(re.findall(r"-(\d+)\.log$", p)[0]))
    return [r for r in (parse(p) for p in paths) if r]


def report(prefix: str) -> dict:
    runs = load(prefix)
    done = [r for r in runs if r["stopped"] == COMPLETED]
    print(f"== {prefix}")
    print(f"{'局':<16}{'层':>4}{'章':>3}  {'死在':<6}{'对手':<22}{'进场血':>6}{'精英':>5}{'剩金':>5}{'牌数':>5}")
    for r in runs:
        print(f"{r['log']:<16}{r['floor']:>4}{r['act'] or '-':>3}  {r['room']:<6}{r['foe']:<22}"
              f"{r['hp_in'] if r['hp_in'] is not None else '-':>6}{r['elites']:>5}{r['gold'] or 0:>5}"
              f"{r['deck_size']:>5}" + ("" if r in done else f"  ⚠ {r['stopped']}"))
    if not done:
        return {}
    floors = [r["floor"] for r in done]
    stats = {"n": len(done), "mean": statistics.mean(floors), "median": statistics.median(floors),
             "sd": statistics.pstdev(floors), "act1_deaths": sum(1 for r in done if r["act"] == 1),
             "gold_left": statistics.mean(r["gold"] or 0 for r in done),
             "elites": statistics.mean(r["elites"] for r in done)}
    print(f"\n完成 {stats['n']} 局：平均 {stats['mean']:.1f} 层，中位 {stats['median']}，最好 {max(floors)}，"
          f"最差 {min(floors)}，标准差 {stats['sd']:.1f}")
    print("死在：", dict(Counter(r["room"] for r in done)))
    print("章节：", dict(Counter(r["act"] for r in done)))
    print("对手：", dict(Counter(r["foe"] for r in done).most_common()))
    print(f"平均打精英 {stats['elites']:.1f} 个，死时平均剩金 {stats['gold_left']:.0f}")
    by_room: dict[str, list[int]] = defaultdict(list)
    for r in done:
        for room, loss in r["losses"]:
            by_room[room].append(loss)
    if by_room:
        print("每场掉血：", {k: f"{statistics.mean(v):.1f}（{len(v)} 场）" for k, v in by_room.items()})
    return stats


if __name__ == "__main__":
    groups = sys.argv[1:] or ["baseline"]
    results = [report(g) for g in groups]
    if len(results) == 2 and all(results):
        a, b = results
        print(f"\n对比 {groups[0]} vs {groups[1]}：平均层数 {a['mean']:.1f} vs {b['mean']:.1f}"
              f"（差 {a['mean'] - b['mean']:+.1f}），第一章死亡 {a['act1_deaths']}/{a['n']} vs "
              f"{b['act1_deaths']}/{b['n']}，剩金 {a['gold_left']:.0f} vs {b['gold_left']:.0f}")
