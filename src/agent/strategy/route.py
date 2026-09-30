"""地图路线：枚举本章所有走到 Boss 的路径，沿途推演血量，取收益减风险最高的一条。

每类房间两个数：
- **期望掉血**：社区对局统计里该章、该类房间的平均掉血（metrics.json 的 encounters）；
- **收益**：折算成「血量当量」—— 精英给遗物，休息点回血或升级，商店看金币，等等。

血越少，同样的掉血越致命（权重随缺血程度上升）；推演中血量归零的路径直接出局。
这就是「前期先打几场小怪再碰精英」「血少就绕开精英、往休息点走」这些攻略经验
的来源 —— 不是写死的规则，而是推演的结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from ..gamedata import db

MAX_PATHS = 20000
DAMAGE_SCALE = 0.7

# 数据缺失时的兜底期望掉血（按章）
FALLBACK_DAMAGE = {"Monster": (7, 11, 14), "Elite": (22, 28, 32), "Boss": (40, 55, 70)}


@lru_cache(maxsize=None)
def expected_damage(room: str, act: int) -> float:
    """社区统计：该章该类房间的平均掉血。"""
    kind = {"Monster": "monster", "Elite": "elite", "Boss": "boss"}.get(room)
    if not kind:
        return 0.0
    vals = [e["avg_damage"] for e in db().encounters.values()
            if e.get("room") == kind and e.get("act") == act and e.get("avg_damage") is not None]
    if vals:
        # 社区平均含大量弱势对局；成型牌组实际掉血明显更少
        return DAMAGE_SCALE * sum(vals) / len(vals)
    return FALLBACK_DAMAGE[room][min(act, 3) - 1]


@dataclass
class RunView:
    hp: int
    max_hp: int
    gold: int
    act: int
    floor: int                   # 本章第几层
    deck_size: int
    potions_free: int


def _loss_weight(hp: float, max_hp: int) -> float:
    return 1.0 + 2.5 * (1 - max(0.0, hp) / max_hp) ** 2


def _step(room: str, hp: float, v: RunView, gold: float, fights: int) -> tuple[float, float, float]:
    """进这个房间：返回 (收益, 新血量, 新金币)。"""
    reward = 0.0
    if room == "Monster":
        dmg = expected_damage("Monster", v.act)
        reward = (7.0 if v.act == 1 else 5.0)
        gold += 15
    elif room == "Elite":
        # 牌组还没成型时精英更危险：本章小怪打得越少，掉血越多
        dmg = expected_damage("Elite", v.act) * (1.35 if fights < 2 and v.act == 1 else 1.0)
        reward = 28.0             # 遗物 + 更好的卡牌奖励
        gold += 30
    elif room == "RestSite":
        dmg = 0.0
        # 回血还是升级：两者都折成分数，取大的
        healed = min(0.3 * v.max_hp, v.max_hp - hp)
        heal_value = healed * _loss_weight(hp, v.max_hp) * 0.6
        if heal_value > 8.0:
            reward, hp = heal_value, hp + healed
        else:
            reward = 8.0              # 升级一张牌
    elif room == "Shop":
        dmg = 0.0
        reward = min(12.0, gold / 16.0) + (3.0 if v.deck_size >= 15 else 0.0)
        gold = max(0.0, gold - 150)
    elif room == "Treasure":
        dmg, reward = 0.0, 13.0
    elif room == "Unknown":
        dmg = 0.35 * expected_damage("Monster", v.act)
        reward = 5.0
    elif room == "Boss":
        # Boss 躲不掉：不按「打死/没打死」截断，只按到达时的血量计风险
        dmg = expected_damage("Boss", v.act)
        return -dmg * _loss_weight(hp - dmg / 2, v.max_hp) - max(0.0, dmg - hp) * 3, hp, gold
    else:
        dmg, reward = 0.0, 1.0
    cost = dmg * _loss_weight(hp - dmg / 2, v.max_hp)
    return reward - cost, hp - dmg, gold


def best_move(state: dict) -> tuple[int | None, str]:
    """返回 map.options 里该走的下标与理由。"""
    m = state.get("map") or {}
    options = m.get("options") or []
    if not options:
        return None, "没有可走的节点"
    if len(options) == 1:
        return options[0]["i"], f"只有一条路（{options[0].get('type')}）"

    graph: dict[tuple[int, int], tuple[str, list[tuple[int, int]]]] = {}
    for r, c, t, nxt in m.get("nodes") or []:
        graph[(r, c)] = (t, [tuple(x) for x in nxt])

    v = _view(state)
    best_i, best_score, best_path = options[0]["i"], float("-inf"), []
    for o in options:
        start = (o["row"], o["col"])
        if start not in graph:
            graph[start] = (o.get("type") or "Unknown", [])
        score, path = _best_from(start, graph, v)
        if score > best_score:
            best_i, best_score, best_path = o["i"], score, path
    route = "→".join(_short(t) for t in best_path)
    return best_i, f"路线 {route}（评分 {best_score:+.1f}，血 {v.hp}/{v.max_hp}，金 {v.gold}）"


def _best_from(start, graph, v: RunView) -> tuple[float, list[str]]:
    best = [float("-inf"), []]
    count = [0]

    def dfs(node, hp, gold, fights, acc, path):
        if count[0] >= MAX_PATHS:
            return
        room, nxt = graph.get(node, ("Unknown", []))
        gain, hp2, gold2 = _step(room, hp, v, gold, fights)
        total = acc + gain
        path = path + [room]
        # 推演里「死了」不截断路径：截断会让先死的路线少算后面的代价，
        # 而当所有路线都推演到死时，又会变成谁排第一走谁。改为按缺口扣分、
        # 血量压到 1 继续推演，危险程度仍然可比。
        if hp2 <= 0 and room != "Boss":
            total -= 40.0 + 6.0 * (1 - hp2)
            hp2 = 1.0
        if not nxt:
            count[0] += 1
            if total > best[0]:
                best[0], best[1] = total, path
            return
        f2 = fights + (1 if room in ("Monster", "Elite") else 0)
        for child in nxt:
            dfs(child, hp2, gold2, f2, total, path)

    dfs(start, float(v.hp), float(v.gold), _fights_so_far(v), 0.0, [])
    return best[0], best[1]


def _fights_so_far(v: RunView) -> int:
    return max(0, v.floor - 1)


def _view(state: dict) -> RunView:
    run, player = state.get("run") or {}, state.get("player") or {}
    potions = state.get("potions") or []
    return RunView(hp=player.get("hp") or 1, max_hp=player.get("max_hp") or 1,
                   gold=run.get("gold") or 0, act=run.get("act") or 1, floor=run.get("floor") or 0,
                   deck_size=len(state.get("deck") or []), potions_free=sum(1 for p in potions if not p))


def _short(room: str) -> str:
    return {"Monster": "怪", "Elite": "精英", "RestSite": "火", "Shop": "店", "Treasure": "箱",
            "Unknown": "?", "Boss": "Boss", "Ancient": "古"}.get(room, room)
