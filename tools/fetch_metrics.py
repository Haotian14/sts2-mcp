"""抓取社区对局统计 → src/agent/data/metrics.json

来源：spire-codex.com（约 190 万局真实对局的聚合；API 文档见 /openapi.json）。

- cards / relics / potions：Elo（「同时被提供时更常被选」的偏好强度）、选取率、
  选取后的胜率，以及卡牌升级版的胜率；
- archetypes：每个角色聚类出的主流构筑（定义牌、定义遗物、胜率、占比）；
- encounters：每场遭遇的致死率、平均掉血、平均回合数。

运行时**不联网**：抓一次、存进仓库，策略只读本地快照。

用法：
    python tools/fetch_metrics.py
"""

from __future__ import annotations

import json
import os
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "src", "agent", "data", "metrics.json")
API = "https://spire-codex.com/api"
CHARACTERS = ("ironclad", "silent", "defect", "necrobinder", "regent")


def get(path: str) -> dict:
    req = urllib.request.Request(f"{API}/{path}", headers={"User-Agent": "sts2-agent"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def entity_table(kind: str) -> dict:
    """Elo 与选取率只在不按角色过滤的表里有；卡牌基本是角色专属的，全局表即可。"""
    data = get(f"runs/metrics/{kind}")
    out: dict[str, dict] = {}
    for row in data["rows"]:
        entry = out.setdefault(row["id"], {})
        if row.get("upgraded"):
            entry["win_up"] = row.get("win_rate")
            entry["n_up"] = row.get("picks")
            continue
        entry.update({
            "elo": row.get("elo"),
            "win": row.get("win_rate"),
            "pick": row.get("pick_rate"),
            "pick_by_act": row.get("pick_rate_by_act"),
            "n": row.get("picks"),
        })
    return {"baseline_win": data.get("baseline_win_rate"), "total_runs": data.get("total_runs"),
            "rows": out}


def archetypes() -> dict:
    out: dict[str, list] = {}
    for c in CHARACTERS:
        data = get(f"runs/archetypes?character={c}&limit=20")
        for char, builds in (data.get("characters") or {}).items():
            if char.lower() != c:
                continue
            out[c] = [{
                "name": b["name"],
                "share": b.get("share"),
                "win": b.get("win_rate"),
                "cards": [x["id"] for x in b.get("defining_cards") or []],
                "relics": [x["id"] for x in b.get("defining_relics") or []],
            } for b in builds]
    return out


def encounters() -> dict:
    data = get("runs/encounter-stats?limit=500")
    out = {}
    for e in data.get("encounters") or []:
        total = e.get("total") or 0
        out[e["encounter_id"]] = {
            "act": e.get("act"),
            "room": e.get("room_type"),
            "fatal": round((e.get("fatal") or 0) / total, 4) if total else None,
            "avg_damage": e.get("avg_damage"),
            "avg_turns": e.get("avg_turns"),
        }
    return out


def main() -> None:
    result = {
        "source": "spire-codex.com",
        "cards": entity_table("cards"),
        "relics": entity_table("relics"),
        "potions": entity_table("potions"),
        "archetypes": archetypes(),
        "encounters": encounters(),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"cards {len(result['cards']['rows'])}  relics {len(result['relics']['rows'])}  "
          f"potions {len(result['potions']['rows'])}  encounters {len(result['encounters'])}  "
          f"archetypes {sum(len(v) for v in result['archetypes'].values())}")


if __name__ == "__main__":
    main()
