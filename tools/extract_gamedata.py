"""从游戏本体抽取静态数据 → src/agent/data/{cards,relics,potions,powers}.json

数据来源只有两处，都在本机游戏目录里：

1. sts2.dll 反编译源码（ilspycmd）—— 费用、类型、稀有度、目标、变量、关键词、
   升级改动，以及 OnPlay / OnUse 里的效果序列；
2. SlayTheSpire2.pck 里的 localization/{eng,zhs}/*.json —— 名字与卡面文本。

效果序列只抽「结构」（打几段、打谁、给谁上什么能力），数值一律留变量名：
战斗里真正的数字来自 /state 的 hand[].values（已含力量、易伤、升级等修正）。

用法：
    python tools/extract_gamedata.py                 # 自动找游戏目录、自动反编译
    python tools/extract_gamedata.py --decomp DIR    # 复用已有的反编译输出
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "src", "agent", "data")

GAME_CANDIDATES = [
    r"D:\SteamLibrary\steamapps\common\Slay the Spire 2",
    r"C:\Program Files (x86)\Steam\steamapps\common\Slay the Spire 2",
]

CHARACTERS = ("Ironclad", "Silent", "Defect", "Necrobinder", "Regent")

# 变量类 → /state 里 values 的键名
VAR_NAMES = {
    "DamageVar": "Damage", "BlockVar": "Block", "CardsVar": "Cards", "EnergyVar": "Energy",
    "RepeatVar": "Repeat", "HpLossVar": "HpLoss", "CalculationBaseVar": "CalculationBase",
    "CalculationExtraVar": "CalculationExtra", "ExtraDamageVar": "ExtraDamage",
    "CalculatedDamageVar": "CalculatedDamage", "CalculatedVar": "Calculated",
    "CalculatedBlockVar": "CalculatedBlock", "StarsVar": "Stars", "SummonVar": "Summon",
    "ForgeVar": "Forge", "OstyDamageVar": "OstyDamage", "GoldVar": "Gold",
    "MaxHpVar": "MaxHp", "HealVar": "Heal",
}


# --------------------------------------------------------------------------
#  输入
# --------------------------------------------------------------------------


def find_game() -> str:
    for p in GAME_CANDIDATES:
        if os.path.isfile(os.path.join(p, "SlayTheSpire2.exe")):
            return p
    sys.exit("找不到游戏目录，请用 --game 指定")


def decompile(game: str) -> str:
    dll = os.path.join(game, "data_sts2_windows_x86_64", "sts2.dll")
    out = os.path.join(tempfile.gettempdir(), "sts2-decomp")
    if not os.path.isdir(os.path.join(out, "MegaCrit.Sts2.Core.Models.Cards")):
        env = dict(os.environ, DOTNET_ROLL_FORWARD="Major")
        subprocess.run(["ilspycmd", "-p", "-o", out, dll], check=True, env=env,
                       stdout=subprocess.DEVNULL)
    return out


def read_localization(game: str) -> dict[str, dict[str, dict[str, str]]]:
    """Godot 4 pck（格式 v3）里取 localization/{eng,zhs}/*.json，不解包整个 2GB 文件。"""
    loc: dict[str, dict[str, dict[str, str]]] = {"eng": {}, "zhs": {}}
    with open(os.path.join(game, "SlayTheSpire2.pck"), "rb") as f:
        magic, ver = struct.unpack("<4sI", f.read(8))
        if magic != b"GDPC" or ver != 3:
            sys.exit(f"不认识的 pck 格式 {magic!r} v{ver}")
        f.read(12)                                   # 引擎版本
        _flags, base, dir_ofs = struct.unpack("<IQQ", f.read(20))
        f.seek(dir_ofs)
        entries = []
        for _ in range(struct.unpack("<I", f.read(4))[0]):
            n = struct.unpack("<I", f.read(4))[0]
            path = f.read(n).rstrip(b"\0").decode()
            ofs, size = struct.unpack("<QQ", f.read(16))
            f.read(20)                               # md5 + flags
            entries.append((path, base + ofs, size))
        for path, ofs, size in entries:
            parts = path.split("/")
            if len(parts) == 3 and parts[0] == "localization" and parts[1] in loc \
                    and parts[2].endswith(".json"):
                f.seek(ofs)
                loc[parts[1]][parts[2][:-5]] = json.loads(f.read(size))
    return loc


# --------------------------------------------------------------------------
#  C# 源码解析
# --------------------------------------------------------------------------


def snake(name: str) -> str:
    """StrikeSilent → STRIKE_SILENT（本地化与社区数据用的键）。"""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return s.upper()


def method_body(src: str, name: str) -> str:
    m = re.search(rf"\b{name}\s*\([^)]*\)\s*\n?\s*\{{", src)
    if not m:
        return ""
    depth, i = 0, m.end() - 1
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
    return ""


def parse_vars(src: str) -> dict[str, float]:
    m = re.search(r"CanonicalVars\s*=>(.*?);\s*\n", src, re.S)
    if not m:
        return {}
    text = m.group(1)
    out: dict[str, float] = {}
    for vm in re.finditer(r"new (\w+?)(?:<(\w+)>)?\(([^()]*(?:\([^()]*\))?[^()]*)\)", text):
        cls, generic, args = vm.groups()
        named = re.match(r'\s*"(\w+)"\s*,\s*(-?\d+(?:\.\d+)?)m?', args)
        num = re.search(r"(-?\d+(?:\.\d+)?)m?", args)
        if named:
            out[named.group(1)] = float(named.group(2))
        elif cls == "PowerVar" and generic:
            out[generic] = float(num.group(1)) if num else 0.0
        elif cls in VAR_NAMES:
            out[VAR_NAMES[cls]] = float(num.group(1)) if num else 0.0
    return out


def resolve_var(expr: str, vars_: dict[str, float]) -> str | int | None:
    """`base.DynamicVars.Weak.BaseValue` → "WeakPower"；字面量 → int。"""
    m = re.search(r'DynamicVars\["(\w+)"\]', expr)
    if m:
        return m.group(1)
    m = re.search(r"DynamicVars\.(\w+)", expr)
    if m:
        acc = m.group(1)
        for cand in (acc, acc + "Power", VAR_NAMES.get(acc + "Var", "")):
            if cand in vars_:
                return cand
        return acc
    m = re.match(r"\s*(-?\d+)m?\s*$", expr)
    if m:
        return int(m.group(1))
    return None


# 不影响结算的条件：动画速度、本地玩家判定、空值保护、角色专属动作
COSMETIC_IF = re.compile(r"SaveManager|FastMode|LocalContext|TestMode|nCreature|vfxNode|"
                         r"Character is|[!=]= null\)|IsMe")

TARGET_OF = {"AnyEnemy": "enemy", "AllEnemies": "all", "RandomEnemy": "random"}


def parse_effects(body: str, vars_: dict[str, float],
                  card_target: str | None = None) -> tuple[list[dict], bool]:
    """按出现顺序抽效果。返回 (效果, 是否含没建模的逻辑)。"""
    effects: list[tuple[int, dict]] = []
    complex_ = False

    loops_enemies = bool(re.search(r"foreach \([^)]*\bin\b[^)]*(HittableEnemies|Enemies|GetOpponentsOf)", body))

    for m in re.finditer(r"DamageCmd\.Attack\((.*?)\)(.*?)\.Execute\(", body, re.S):
        chain = m.group(2)
        hits: str | int | None = 1
        hm = re.search(r"WithHitCount\(([^()]*(?:\([^()]*\))?)\)", chain)
        if hm:
            hits = "X" if "ResolveEnergyXValue" in hm.group(1) else resolve_var(hm.group(1), vars_)
            if hits is None:
                complex_ = True
                hits = 1
        if card_target in TARGET_OF:
            tgt = TARGET_OF[card_target]      # 牌自己的目标类型最可靠（小刀的 if/else 见 Shiv.cs）
        elif "TargetingAllOpponents" in chain:
            tgt = "all"
        elif "TargetingRandomOpponents" in chain:
            tgt = "random"
        else:
            tgt = "enemy"
        effects.append((m.start(), {"op": "attack", "var": resolve_var(m.group(1), vars_) or "Damage",
                                    "hits": hits, "to": tgt}))

    for m in re.finditer(r"CreatureCmd\.GainBlock\(([^,]*),\s*([^,)]*)", body):
        effects.append((m.start(), {"op": "block", "var": resolve_var(m.group(2), vars_) or "Block"}))

    for m in re.finditer(r"PowerCmd\.Apply<(\w+)>\(\s*(?:choiceContext|new \w+\(\))\s*,\s*([^,]*),\s*([^,]*),", body):
        power, who, amt = m.groups()
        if "Owner.Creature" in who:
            to = "self"
        elif "cardPlay.Target" in who or who.strip() == "target":
            to = "enemy"
        elif loops_enemies or "Enemies" in who:
            to = "all"
        else:
            to = "all" if loops_enemies else "other"
        amount = resolve_var(amt, vars_)
        if amount is None:
            amount = power if power in vars_ else None
            complex_ = complex_ or amount is None
        effects.append((m.start(), {"op": "apply", "power": power, "var": amount, "to": to}))

    for m in re.finditer(r"CardPileCmd\.Draw\([^,]*,\s*([^,)]*)", body):
        effects.append((m.start(), {"op": "draw", "var": resolve_var(m.group(1), vars_) or "Cards"}))
    for m in re.finditer(r"PlayerCmd\.GainEnergy\(([^,)]*)", body):
        effects.append((m.start(), {"op": "energy", "var": resolve_var(m.group(1), vars_) or "Energy"}))
    for m in re.finditer(r"CreatureCmd\.Damage\([^,]*,\s*base\.Owner\.Creature,\s*([^,)]*)", body):
        effects.append((m.start(), {"op": "lose_hp", "var": resolve_var(m.group(1), vars_) or "HpLoss"}))
    for m in re.finditer(r"CreatureCmd\.Heal\([^,]*,\s*([^,)]*)", body):
        effects.append((m.start(), {"op": "heal", "var": resolve_var(m.group(1), vars_) or "Heal"}))

    # 生成牌：Shiv.CreateInHand(...) 或 CreateCard<X>
    for m in re.finditer(r"(\w+)\.CreateInHand\(", body):
        count: str | int = 1
        loop = re.search(r"for \(int \w+ = 0; \w+ < ([^;]+);", body[:m.start()])
        if loop:
            count = resolve_var(loop.group(1), vars_) or 1
        effects.append((m.start(), {"op": "make", "card": m.group(1), "count": count, "pile": "hand"}))
    for m in re.finditer(r"CreateCard<(\w+)>", body):
        pile = re.search(r"PileType\.(\w+)", body[m.start():m.start() + 300])
        effects.append((m.start(), {"op": "make", "card": m.group(1), "count": 1,
                                    "pile": (pile.group(1).lower() if pile else "hand")}))

    for m in re.finditer(r"CardSelectCmd\.(\w+)\(", body):
        kind = m.group(1)
        nearby = body[m.start():m.start() + 600]
        if kind == "FromHandForDiscard" or "CardCmd.Discard" in nearby:
            op = "discard"
        elif "CardCmd.Exhaust" in nearby:
            op = "exhaust"
        elif "CardCmd.Upgrade" in nearby:
            op = "upgrade"
        elif kind == "FromCombatPile" or "PileType.Draw" in nearby or "PileType.Discard" in nearby:
            op = "fetch"
        else:
            op = "select"
        effects.append((m.start(), {"op": "choose", "kind": op}))

    for kw, op in (("PlayerCmd.GainStars", "stars"), ("OrbCmd.Channel", "orb"),
                   ("OstyCmd.Summon", "summon"), ("ForgeCmd.Forge", "forge"),
                   ("PlayerCmd.EndTurn", "end_turn"), ("CardCmd.AutoPlay", "autoplay")):
        for m in re.finditer(re.escape(kw), body):
            effects.append((m.start(), {"op": op}))

    # 我们只「认识」上面这些命令。条件分支、自定义循环、其它命令 → 标记为复杂，
    # 规划器对复杂牌退回到静态估值，而不是按不完整的效果去模拟。
    stripped = re.sub(r"(TriggerAnim|Sfx|Vfx|Cmd\.Wait|ThrowIfNull|AssertValid)[^;]*;", "", body)
    known = ("DamageCmd.Attack", "CreatureCmd.GainBlock", "PowerCmd.Apply", "CardPileCmd.Draw",
             "PlayerCmd.GainEnergy", "CreateInHand", "CardSelectCmd", "CardCmd.Discard",
             "CardCmd.Exhaust", "CreatureCmd.Damage", "CreatureCmd.Heal", "CreateCard",
             "AddGeneratedCardToCombat", "CardCmd.Upgrade")
    other_cmds = {c for c in re.findall(r"\b(\w+Cmd\.\w+)", stripped)
                  if not any(c.startswith(k) or k.startswith(c) for k in known)
                  and not c.startswith(("SfxCmd", "VfxCmd"))}
    conditions = [c for c in re.findall(r"\bif \((.*)", stripped) if not COSMETIC_IF.search(c)]
    if other_cmds or conditions or re.search(r"\bswitch\b|\bwhile\b", stripped):
        complex_ = True

    return [e for _, e in sorted(effects, key=lambda t: t[0])], complex_


def parse_upgrade(src: str, vars_: dict[str, float]) -> dict:
    body = method_body(src, "OnUpgrade")
    up: dict = {}
    for m in re.finditer(r"DynamicVars(?:\.(\w+)|\[\"(\w+)\"\])\.UpgradeValueBy\((-?[\d.]+)m?\)", body):
        key = resolve_var(f"DynamicVars.{m.group(1)}" if m.group(1) else f'DynamicVars["{m.group(2)}"]', vars_)
        up.setdefault("vars", {})[str(key)] = float(m.group(3))
    m = re.search(r"EnergyCost\.UpgradeBy\((-?\d+)\)", body)
    if m:
        up["cost"] = int(m.group(1))
    add = re.findall(r"AddKeyword\(CardKeyword\.(\w+)\)", body)
    rem = re.findall(r"RemoveKeyword\(CardKeyword\.(\w+)\)", body)
    if add:
        up["add_keywords"] = add
    if rem:
        up["remove_keywords"] = rem
    return up


def pool_members(decomp: str, folder: str, suffix: str, kind: str) -> dict[str, str]:
    """类名 → 所属池（Silent / Colorless / Curse / Status / Token / Event / Shared …）。"""
    d = os.path.join(decomp, folder)
    owner: dict[str, str] = {}
    for fn in os.listdir(d):
        if not fn.endswith(suffix + ".cs"):
            continue
        pool = fn[: -len(suffix + ".cs")]
        src = open(os.path.join(d, fn), encoding="utf-8").read()
        for name in re.findall(rf"ModelDb\.{kind}<(\w+)>", src):
            owner.setdefault(name, pool)
    return owner


def loc_entry(loc: dict, table: str, key: str) -> dict:
    out = {}
    for lang in ("eng", "zhs"):
        t = loc[lang].get(table, {})
        if f"{key}.title" in t:
            out["name" if lang == "eng" else "name_zh"] = t[f"{key}.title"]
        if f"{key}.description" in t:
            out["text" if lang == "eng" else "text_zh"] = t[f"{key}.description"]
    return out


# --------------------------------------------------------------------------
#  各类实体
# --------------------------------------------------------------------------


def extract_cards(decomp: str, loc: dict) -> dict:
    d = os.path.join(decomp, "MegaCrit.Sts2.Core.Models.Cards")
    pools = pool_members(decomp, "MegaCrit.Sts2.Core.Models.CardPools", "CardPool", "Card")
    cards = {}
    for fn in sorted(os.listdir(d)):
        src = open(os.path.join(d, fn), encoding="utf-8").read()
        m = re.search(r"public sealed class (\w+) : CardModel", src)
        if not m:
            continue
        name = m.group(1)
        ctor = re.search(rf"public {name}\(\)\s*:\s*base\((-?\d+),\s*CardType\.(\w+),\s*CardRarity\.(\w+),\s*TargetType\.(\w+)", src)
        if not ctor:
            continue
        pool = pools.get(name)
        if pool in (None, "Deprecated", "Mock"):
            continue
        vars_ = parse_vars(src)
        kws = re.search(r"CanonicalKeywords\s*=>(.*?);\s*\n", src, re.S)
        effects, complex_ = parse_effects(method_body(src, "OnPlay"), vars_, ctor.group(4))
        card = {
            "key": snake(name),
            "pool": pool,
            "cost": "X" if "HasEnergyCostX => true" in src else int(ctor.group(1)),
            "type": ctor.group(2),
            "rarity": ctor.group(3),
            "target": ctor.group(4),
            "vars": vars_,
            "keywords": re.findall(r"CardKeyword\.(\w+)", kws.group(1)) if kws else [],
            "effects": effects,
        }
        if complex_:
            card["complex"] = True
        up = parse_upgrade(src, vars_)
        if up:
            card["upgrade"] = up
        # 回合结束/抽到/弃掉时触发的被动（例：进阶之灾、奇巧以外的钩子）
        hooks = sorted(set(re.findall(r"override async Task (On\w+|After\w+|Before\w+)\(", src)) - {"OnPlay"})
        if hooks:
            card["hooks"] = hooks
        card.update(loc_entry(loc, "cards", card["key"]))
        cards[name] = card
    return cards


def extract_simple(decomp: str, loc: dict, folder: str, base: str, table: str,
                   pools: dict[str, str] | None = None, body: str | None = None) -> dict:
    d = os.path.join(decomp, folder)
    out = {}
    for fn in sorted(os.listdir(d)):
        src = open(os.path.join(d, fn), encoding="utf-8").read()
        m = re.search(rf"public sealed class (\w+) : {base}\b", src)
        if not m:
            continue
        name = m.group(1)
        if pools is not None and pools.get(name) in (None, "Deprecated", "Mock"):
            continue
        e: dict = {"key": snake(name)}
        if pools is not None:
            e["pool"] = pools[name]
        for prop in ("Rarity", "Type", "StackType", "Usage", "TargetType"):
            pm = re.search(rf"override \w+ {prop} => \w+\.(\w+);", src)
            if pm:
                e[prop.lower()] = pm.group(1)
        vars_ = parse_vars(src)
        if vars_:
            e["vars"] = vars_
        if body:
            effects, complex_ = parse_effects(method_body(src, body), vars_)
            e["effects"] = effects
            if complex_:
                e["complex"] = True
        e.update(loc_entry(loc, table, e["key"]))
        out[name] = e
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game")
    ap.add_argument("--decomp")
    args = ap.parse_args()

    game = args.game or find_game()
    decomp = args.decomp or decompile(game)
    loc = read_localization(game)
    version = json.load(open(os.path.join(game, "release_info.json")))["version"]

    relic_pools = pool_members(decomp, "MegaCrit.Sts2.Core.Models.RelicPools", "RelicPool", "Relic")
    potion_pools = {n: "Shared" for n in re.findall(
        r"public sealed class (\w+) : PotionModel",
        "\n".join(open(os.path.join(decomp, "MegaCrit.Sts2.Core.Models.Potions", f), encoding="utf-8").read()
                  for f in os.listdir(os.path.join(decomp, "MegaCrit.Sts2.Core.Models.Potions"))))
                    if n not in ("DeprecatedPotion",)}

    data = {
        "cards": extract_cards(decomp, loc),
        "relics": extract_simple(decomp, loc, "MegaCrit.Sts2.Core.Models.Relics", "RelicModel", "relics",
                                 pools=relic_pools),
        "potions": extract_simple(decomp, loc, "MegaCrit.Sts2.Core.Models.Potions", "PotionModel", "potions",
                                  pools=potion_pools, body="OnUse"),
        "powers": extract_simple(decomp, loc, "MegaCrit.Sts2.Core.Models.Powers", "PowerModel", "powers"),
    }
    os.makedirs(OUT, exist_ok=True)
    for kind, table in data.items():
        with open(os.path.join(OUT, f"{kind}.json"), "w", encoding="utf-8") as f:
            json.dump({"game_version": version, kind: table}, f, ensure_ascii=False, indent=1, sort_keys=True)
        n_complex = sum(1 for v in table.values() if v.get("complex"))
        print(f"{kind}: {len(table)}（其中 {n_complex} 个含未建模逻辑）")


if __name__ == "__main__":
    main()
