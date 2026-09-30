"""静态数据：游戏本体抽出的卡牌/遗物/药水/能力定义 + 社区对局统计。

数据文件由 tools/extract_gamedata.py 与 tools/fetch_metrics.py 生成，见 data/。
键一律是游戏类型短名（`StrikeSilent`），与 /state 的 id 同源；
社区统计用的 `STRIKE_SILENT` 形式由 `key` 字段换算。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

DATA = os.path.join(os.path.dirname(__file__), "data")


def _load(name: str) -> dict[str, Any]:
    with open(os.path.join(DATA, f"{name}.json"), encoding="utf-8") as f:
        return json.load(f)


@dataclass(frozen=True)
class Metric:
    """社区统计。elo 只有卡牌有；win 是「拿了它的局」的胜率，有幸存者偏差。"""
    elo: float | None = None
    win: float | None = None
    win_up: float | None = None
    n: int = 0
    pick_by_act: tuple[float | None, ...] = ()


class GameData:
    def __init__(self) -> None:
        self.cards: dict[str, dict] = _load("cards")["cards"]
        self.relics: dict[str, dict] = _load("relics")["relics"]
        self.potions: dict[str, dict] = _load("potions")["potions"]
        self.powers: dict[str, dict] = _load("powers")["powers"]
        m = _load("metrics")
        self.baseline_win: float = m["cards"]["baseline_win"] or 43.0
        self._metrics = {k: m[k]["rows"] for k in ("cards", "relics", "potions")}
        self.archetypes: dict[str, list[dict]] = m["archetypes"]
        self.encounters: dict[str, dict] = m["encounters"]
        self._by_key = {v["key"]: k for table in (self.cards, self.relics, self.potions)
                        for k, v in table.items()}

    # ---------------------------------------------------------------- 查询

    def card(self, card_id: str) -> dict:
        """未知的牌返回空定义 —— 调用方按「不认识」处理，而不是崩。"""
        return self.cards.get(base_id(card_id), {})

    def metric(self, kind: str, entity_id: str) -> Metric:
        table = {"cards": self.cards, "relics": self.relics, "potions": self.potions}[kind]
        key = (table.get(base_id(entity_id)) or {}).get("key") or entity_id
        row = self._metrics[kind].get(key)
        if not row:
            return Metric()
        return Metric(elo=row.get("elo"), win=row.get("win"), win_up=row.get("win_up"),
                      n=row.get("n") or 0, pick_by_act=tuple(row.get("pick_by_act") or ()))

    def id_of_key(self, key: str) -> str | None:
        return self._by_key.get(key)

    def name(self, entity_id: str) -> str:
        for table in (self.cards, self.relics, self.potions, self.powers):
            e = table.get(base_id(entity_id))
            if e:
                return e.get("name_zh") or e.get("name") or entity_id
        return entity_id

    def encounter(self, encounter_id: str | None) -> dict:
        if not encounter_id:
            return {}
        return self.encounters.get(snake(encounter_id)) or {}


def base_id(card_id: str) -> str:
    """牌库里升级过的牌以 + 结尾（见桥接层 deck 导出）。"""
    return card_id[:-1] if card_id.endswith("+") else card_id


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).upper()


@lru_cache(maxsize=1)
def db() -> GameData:
    return GameData()
