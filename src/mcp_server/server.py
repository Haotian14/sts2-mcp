"""sts2-mcp —— 把游戏内桥接层包装成 MCP 工具，让 Claude 直接玩《杀戮尖塔 2》。

架构位置：

    Claude Code ──MCP(stdio)──> 本文件 ──HTTP──> Sts2Bridge (游戏进程内 C#)
                                              127.0.0.1:8765

本层刻意做得很薄。判断合法性、等待动作结算、组织状态 —— 这些全在桥接层里，
因为它跑在游戏进程内、能直接读游戏对象。本层只负责三件事：

1. 把 HTTP 接口翻译成 MCP 工具；
2. 缓存 /glossary（卡面文本一局之内不变，重复传输纯属浪费 token）；
3. **把模型必须知道的规则写进工具描述里** —— 下标会变、什么牌要指定目标、
   什么时候不能出手。这些规则模型看不到代码，只能从描述里学。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

import autoplay
import journal

# httpx 默认对每个请求打一条 INFO。stdio 传输下日志走 stderr，不会污染协议，
# 但一局要发几百个请求，会把真正的错误淹掉。降到 WARNING。
logging.getLogger("httpx").setLevel(logging.WARNING)

# --------------------------------------------------------------------------
#  与桥接层的连接
# --------------------------------------------------------------------------

BRIDGE_URL = os.environ.get("STS2MCP_URL", "http://127.0.0.1:8765").rstrip("/")

# 读超时必须大于桥接层等待动作稳定的上限（默认 20 秒）——
# end_turn 要等完整个敌方回合，实测 5~8 秒，多怪回合更久。
# 若这里先超时，动作其实已经下发，模型却收到一个失败，是最坏的情形。
_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)

_client = httpx.Client(timeout=_TIMEOUT)

# 卡面文本一局之内基本不变，缓存之。这正是桥接层把 /state 与 /glossary
# 拆开的理由：若合并，每个决策点都要重传一遍 1.6 KB 的静态文本。
_glossary_cache: dict[str, Any] | None = None

# 最近一次见到的局面。决策日志要记的是**出手前**的局面（选了哪个选项、
# 当时几点血），而动作接口只回报执行后的状态 —— 但上一次调用的「执行后」
# 恰好就是这一次的「执行前」，缓存下来即可，不必为记日志多发一个 HTTP。
_last_state: dict[str, Any] = {}


class BridgeError(RuntimeError):
    """桥接层不可达或返回了协议级错误。"""


def _request(method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{BRIDGE_URL}{path}"
    try:
        response = _client.request(method, url, params=params)
    except httpx.ConnectError as exc:
        raise BridgeError(
            f"连不上游戏内桥接层（{BRIDGE_URL}）。请确认：游戏正在运行；"
            f"Steam 启动选项里设置了 scripts/launch-steam.cmd；"
            f"启动后约 10 秒桥接层才就绪。原始错误：{exc}"
        ) from exc
    except httpx.ReadTimeout as exc:
        raise BridgeError(
            f"桥接层超时未响应（{path}）。动作可能已经下发但未在超时内完成，"
            f"**请先调用 get_state 确认实际发生了什么，不要重发动作**。原始错误：{exc}"
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise BridgeError(f"桥接层返回的不是 JSON（HTTP {response.status_code}）：{response.text[:200]}") from exc

    # 桥接层对「这步不能走」返回 200 + ok:false，对畸形请求才返回 4xx。
    # 前者是正常的游戏结果，要原样交给模型；后者是我们自己调用错了。
    if response.status_code >= 400:
        raise BridgeError(f"桥接层拒绝了请求（HTTP {response.status_code}）：{payload}")

    global _last_state
    snapshot = payload.get("state") if isinstance(payload.get("state"), dict) else payload
    if isinstance(snapshot, dict) and "in_run" in snapshot:
        _last_state = snapshot
    return payload


server = MCPServer(
    name="sts2",
    instructions=(
        "通过游戏内桥接层游玩《杀戮尖塔 2》单机模式。\n"
        "\n"
        "标准循环：get_state 看局面 → 出牌 / 用药水 → 无牌可出时 end_turn。\n"
        "动作工具会自行等到局面稳定，并在返回值里附带执行后的新状态，"
        "**不需要在动作之后再调一次 get_state**。\n"
        "\n"
        "一局开始时调一次 get_glossary 拿到卡面文本，之后不必再调 —— "
        "卡牌效果不会变，重复获取只是浪费。\n"
        "\n"
        "本工具仅用于单机离线游玩，不得用于多人模式。"
    ),
)


# --------------------------------------------------------------------------
#  只读
# --------------------------------------------------------------------------


@server.tool(
    description=(
        "读取当前游戏状态（约 1.5 KB）。这是做任何决策前的唯一依据。\n"
        "\n"
        "关键字段：\n"
        "- `in_run`：**为 false 表示游戏停在主菜单，存档尚未载入**，此时所有局内字段都是空的；"
        "若 `can_resume` 为 true，先调 resume_run。\n"
        "- `in_combat`：是否在战斗中。为 false 时只有 run / player / relics / potions 有内容。\n"
        "- `awaiting_choice`：**为 true 时游戏正等玩家做选择**，此时任何其他动作都会被拒绝。"
        "同时带有 `choice` 字段时用 choose 应答；没有 `choice` 则是桥接层还接管不了的"
        "选择（地图、商店、事件），只能由人操作。\n"
        "- `choice`：待答的选牌。`options[].i` 是下标，`min`/`max` 是要选几张，"
        "`title` / `text` 是每张牌的中文名与效果（候选牌可能不在牌库里，"
        "get_glossary 覆盖不到）。\n"
        "- `screen`：当前弹出的界面。`type` 是界面类型，`options[].i` 用于 pick，"
        "`can_proceed` 为 true 时用 proceed 离开。**界面开着时 `map.can_move` 必为 false**，"
        "处理完按继续才能回到地图。`options` 为空说明桥接层还不支持这个界面，需要人工操作。\n"
        "- `screen.options[].title` / `.text`：**待选物的中文名与效果文本**"
        "（卡牌三选一、商店、宝箱遗物都有）。构筑决策直接看它，不必去查 get_glossary"
        "—— glossary 只覆盖你**已持有**的东西，待选的一个都没有。\n"
        "- `screen.options[].cost`：商店价格；`available` 即买不买得起，"
        "**每次消费后会按新余额重算**。\n"
        "- `map`：当前坐标与下一步可走的节点。`can_move` 为 true 时可用 move 移动，"
        "`options[].type` 给出房间类型（Monster/Elite/RestSite/Shop/Treasure/Boss…）。\n"
        "- `hand[].i`：出牌用的下标。**每打出一张牌，剩余手牌的下标就会重排**，"
        "所以连续出牌时每次都要以最新状态为准，不能沿用旧下标。\n"
        "- `hand[].playable`：能否打出；为 false 时 `reason` 给出原因"
        "（EnergyCostTooHigh / HasUnplayableKeyword 等）。\n"
        "- `hand[].target`：目标类型。**只有 AnyEnemy / AnyAlly 需要指定目标**，"
        "Self / AllEnemies 之类传了目标反而非法。\n"
        "- `hand[].cost`：能量消耗；为 null 表示这张牌不可打出（诅咒牌）。\n"
        "- `hand[].values`：**这张牌此刻打出去的实际数值**，已含力量、虚弱、"
        "虚弱化、遗物、附魔等全部修正（`Damage` 伤害 / `Block` 格挡 / "
        "`VulnerablePower` 施加的层数…）。**算斩杀线一律用它，不要用 "
        "get_glossary 的卡面文本** —— 卡面是不含修正的裸值，且一局只取一次。\n"
        "- `hand[].damage_vs`：对每只敌人的实际伤害，下标与 `enemies[].i` 对齐。"
        "**只在与 `values.Damage` 不同时才出现**（易伤等目标侧修正），没有这个字段"
        "就说明对谁都是 `values.Damage`。\n"
        "- `enemies[].i`：指定目标用的下标。\n"
        "- `enemies[].intents[]`：敌人下回合要做什么。`damage` 是单次伤害、"
        "`total` 是总伤害（已含力量、虚弱等全部修正），**按 total 决定要挡多少**。\n"
        "- `piles`：只有张数，因为抽牌堆是乱序的，逐张列出既无决策价值又极占篇幅。\n"
        "- `warnings`：非空说明某些字段读取失败（多半是游戏更新了），此时该字段为 null。\n"
        "\n"
        "卡牌与遗物用英文类型短名标识（StrikeSilent / CorpseSlug），"
        "对应的中文名与效果文本在 get_glossary 里。"
    )
)
def get_state() -> dict[str, Any]:
    return _request("GET", "/state")


@server.tool(
    description=(
        "获取卡牌与遗物的名称和效果文本，按标识符索引（StrikeSilent → 打击 / 造成6点伤害）。\n"
        "\n"
        "内容一局之内不变，本服务会缓存 —— **一局开始时调一次即可，之后不要重复调用**。\n"
        "拿到牌库里没见过的新卡时，传 refresh=true 重取一次。"
    )
)
def get_glossary(refresh: bool = False) -> dict[str, Any]:
    global _glossary_cache
    if refresh or _glossary_cache is None:
        _glossary_cache = _request("GET", "/glossary")
    return _glossary_cache


# --------------------------------------------------------------------------
#  决策日志（spec.md 6.5）
#
#  落盘的逻辑全在 journal.py。这里只负责一件事：**给动作起一个三天后还看得懂
#  的名字**。日志里记 `pick(2)` 等于没记 —— 下标当场就过期了，得在下发之前
#  从局面里把名字取出来（「剑柄打击」「Elite」「38 金的无情猛攻」）。
# --------------------------------------------------------------------------

_WHY_DOC = (
    "`why` 是一句话理由，会连同当时的局面一起写进决策日志"
    "（`logs/decisions.jsonl`，跨局累积）。构筑与路线决策决定一局的上限，"
    "而下一局重开时唯一还留得下来的就是这份日志 —— **请如实写你真正的理由**，"
    "写「感觉不错」不如不写。"
)


def _named(options: Any, i: int) -> str:
    opt = next((o for o in options or [] if o.get("i") == i), None)
    if not opt:
        return f"#{i}"
    name = opt.get("title") or opt.get("id") or opt.get("type") or f"#{i}"
    cost = opt.get("cost")
    return f"{name}（{cost}金）" if isinstance(cost, int) else str(name)


def _log(before: dict[str, Any], action: str, why: str,
         result: dict[str, Any] | None = None) -> None:
    # 出手前的局面为空（模型没看状态就直接动手）时退回用执行后的状态，
    # 总比记成一条没有上下文的空记录强
    state = before or (result or {}).get("state") or {}
    journal.record(state, action, why)


# --------------------------------------------------------------------------
#  动作
#
#  三个工具形状一致：同步执行，等到局面稳定才返回，返回值里带上新状态。
#  失败时 ok 为 false 且带结构化 error —— 那是「这步不能走」，不是故障，
#  照着 error 改一步重试即可。
# --------------------------------------------------------------------------

_ACTION_RESULT_DOC = (
    "返回值：`ok` 为 true 表示已执行完毕，`state` 是执行后的新状态"
    "（**不必再调 get_state**）。\n"
    "`ok` 为 false 表示这步不能走，`error` 给出原因，`state` 仍是当前状态：\n"
    "- `unplayable`：打不出，`reason` 说明为何（能量不够 / 诅咒牌 / 被敌人封锁）\n"
    "- `bad_target`：该给目标却没给，或不该给却给了\n"
    "- `bad_index`：下标越界 —— 多半是沿用了过期的下标，重新读状态\n"
    "- `not_ready`：不在出牌阶段；`actions_disabled`：游戏正在结算\n"
    "- `awaiting_choice`：游戏在等玩家做选择，此时无法下发任何动作\n"
    "\n"
    "`settled` 为 false 表示局面未在超时内稳定，随附状态可能是中间态，"
    "此时应重新调 get_state 再做决策。"
)


@server.tool(
    description=(
        "打出一张手牌。\n"
        "\n"
        "`card` 取自 get_state 的 `hand[].i`，`target` 取自 `enemies[].i`。\n"
        "**只有 `hand[].target` 为 AnyEnemy 或 AnyAlly 的牌才传 target**，"
        "其余（Self / AllEnemies / None）必须省略，传了会被判为非法。\n"
        "\n"
        "**每打出一张牌，剩余手牌的下标都会重排。**连续出牌时，"
        "请用上一次调用返回的 `state` 里的下标，不要沿用更早的。\n"
        "\n" + _WHY_DOC + "\n"
        "\n" + _ACTION_RESULT_DOC
    )
)
def play_card(card: int, target: int | None = None, why: str = "") -> dict[str, Any]:
    params: dict[str, Any] = {"card": card}
    if target is not None:
        params["target"] = target
    before = _last_state
    hand = before.get("hand") or []
    name = next((c.get("id") for c in hand if c.get("i") == card), None) or f"#{card}"
    if target is not None:
        foe = next((e.get("id") for e in before.get("enemies") or [] if e.get("i") == target), None)
        name = f"{name} → {foe or target}"
    result = _request("POST", "/action/play_card", params)
    _log(before, f"play_card {name}", why, result)
    return result


@server.tool(
    description=(
        "结束当前回合。\n"
        "\n"
        "会一直等到敌方回合走完、新回合的出牌阶段开始才返回，"
        "实测 5~8 秒，多怪回合更久 —— 这是正常的，不要因为慢而重试。\n"
        "若战斗在此期间结束（打赢或阵亡），返回的状态里 `in_combat` 为 false。\n"
        "\n"
        "结束回合前请先确认没有更好的出牌：能量和手牌到回合结束就浪费了。\n"
        "\n" + _ACTION_RESULT_DOC
    )
)
def end_turn(why: str = "") -> dict[str, Any]:
    before = _last_state
    result = _request("POST", "/action/end_turn")
    _log(before, "end_turn", why, result)
    return result


@server.tool(
    description=(
        "使用一瓶药水。\n"
        "\n"
        "`slot` 是 get_state 里 `potions` 数组的下标，该数组保留空槽（null），"
        "所以下标即槽位号。`target` 取自 `enemies[].i`，仅对指定单体敌人的药水需要；"
        "作用于自己的药水省略即可。\n"
        "\n"
        "药水在战斗外也能喝（比如上路前先回血）。\n"
        "\n" + _WHY_DOC + "\n"
        "\n" + _ACTION_RESULT_DOC
    )
)
def use_potion(slot: int, target: int | None = None, why: str = "") -> dict[str, Any]:
    params: dict[str, Any] = {"slot": slot}
    if target is not None:
        params["target"] = target
    before = _last_state
    potions = before.get("potions") or []
    name = potions[slot] if 0 <= slot < len(potions) and potions[slot] else f"#{slot}"
    result = _request("POST", "/action/use_potion", params)
    _log(before, f"use_potion {name}", why, result)
    return result


# --------------------------------------------------------------------------


@server.tool(
    description=(
        "点击当前界面上的一个选项。`i` 取自 get_state 的 `screen.options[].i`。\n"
        "\n"
        "各类界面共用这一个动作：\n"
        "- `NRewardsScreen` 战斗奖励：`id` 为 GoldReward 金币 / PotionReward 药水 /\n"
        "  RelicReward 遗物 / CardReward 卡牌三选一。**金币和遗物永远该拿**；"
        "药水栏满时药水会不可领取（`available` 为 false）。\n"
        "  **不想要卡牌就别点 CardReward，直接 proceed** —— 牌组越薄，关键牌抽到得越勤。\n"
        "- `NCardRewardSelectionScreen` 卡牌三选一：点一张即加入牌组。\n"
        "- `NMerchantRoom` 商店：点槽位即购买，`cost` 是价格、`available` 是买不买得起。\n"
        "  `MerchantCardRemovalEntry` 是除卡服务 —— 点它之后会转成一次选牌，用 choose 应答。\n"
        "- `NTreasureRoom` 宝箱：先点 Chest 开箱，再点里面的遗物。\n"
        "\n"
        "**待选物的 `title` / `effect 文本`就在 `screen.options[]` 上**，不必去查 "
        "get_glossary（它只覆盖已持有的东西）。卡牌奖励、除卡、商店这类**构筑决策"
        "决定一局的上限**，比任何出牌技巧都重要，务必读完文本再选。\n"
        "\n"
        "领完想要的之后，用 proceed 离开回到地图。\n"
        "\n" + _WHY_DOC + "\n"
        "\n" + _ACTION_RESULT_DOC
    )
)
def pick(i: int, why: str = "") -> dict[str, Any]:
    before = _last_state
    label = _named((before.get("screen") or {}).get("options"), i)
    result = _request("POST", "/action/pick", {"i": i})
    _log(before, f"pick {label}", why, result)
    return result


@server.tool(
    description=(
        "按当前界面上的「继续」按钮，离开该界面回到地图。\n"
        "\n"
        "只有在 get_state 的 `screen.can_proceed` 为 true 时才可用。"
        "**没按继续就回不到地图**，`map.can_move` 会一直是 false。\n"
        "\n"
        "跳过一份卡牌奖励（不点 CardReward 直接按继续）也是一次构筑决策，"
        "值得在 `why` 里写明为什么不要。\n"
        "\n" + _ACTION_RESULT_DOC
    )
)
def proceed(why: str = "") -> dict[str, Any]:
    before = _last_state
    result = _request("POST", "/action/proceed")
    _log(before, "proceed", why, result)
    return result


@server.tool(
    description=(
        "在地图上移动到下一个房间。\n"
        "\n"
        "`node` 取自 get_state 的 `map.options[].i`，仅当 `map.can_move` 为 true 时可用"
        "（即当前停在地图界面上）。每个选项带 `type`：\n"
        "Monster 普通战斗 / Elite 精英（高风险高回报）/ RestSite 休息点（回血或升级）/\n"
        "Shop 商店 / Treasure 宝箱 / Boss / Ancient / Unknown 未知。\n"
        "\n"
        "选路是整局最关键的决策之一：残血时避开精英、想强化牌组时抓休息点和商店。"
        "**先看 `player.hp`**，再决定敢不敢走精英。\n"
        "\n"
        "会一直等到真的走进新房间才返回；若目标是战斗房，返回时战斗已经开始"
        "（`in_combat` 为 true，且已进入出牌阶段）。\n"
        "\n" + _WHY_DOC + "\n"
        "\n" + _ACTION_RESULT_DOC
    )
)
def move(node: int, why: str = "") -> dict[str, Any]:
    before = _last_state
    label = _named((before.get("map") or {}).get("options"), node)
    result = _request("POST", "/action/move", {"node": node})
    _log(before, f"move → {label}", why, result)
    return result


@server.tool(
    description=(
        "应答一次待答的选牌 —— 弃哪张、检索哪张、留哪张。\n"
        "\n"
        "当 get_state 的 `awaiting_choice` 为 true 且带有 `choice` 字段时使用。"
        "`choice.options[].i` 是可选项的下标，`choice.min` / `choice.max` 是要选的张数；"
        "min 为 0 表示可以传空列表跳过。\n"
        "\n"
        "**在应答之前，其他动作一律无法下发** —— 游戏正停在这一步等回答。\n"
        "\n"
        "`awaiting_choice` 为 true 却没有 `choice` 字段，说明这是桥接层还接管不了的"
        "选择（如地图、商店、事件），只能由人操作。\n"
        "\n" + _WHY_DOC + "\n"
        "\n" + _ACTION_RESULT_DOC
    )
)
def choose(cards: list[int], why: str = "") -> dict[str, Any]:
    before = _last_state
    options = (before.get("choice") or {}).get("options")
    label = "、".join(_named(options, c) for c in cards) or "（空）"
    result = _request("POST", "/action/choose", {"cards": ",".join(str(c) for c in cards)})
    _log(before, f"choose {label}", why, result)
    return result


@server.tool(
    description=(
        "点主菜单的「继续游戏」，载入上一局存档。\n"
        "\n"
        "仅当 get_state 的 `in_run` 为 false 且 `can_resume` 为 true 时可用 —— "
        "游戏刚重启时会停在主菜单，此时没有任何局内状态，**所有字段都是空的**，"
        "必须先载入存档才能继续。\n"
        "\n" + _ACTION_RESULT_DOC
    )
)
def resume_run() -> dict[str, Any]:
    return _request("POST", "/action/resume_run")


@server.tool(
    description=(
        "**自动打完当前这场战斗**，直到战斗结束，或局面变险时停手把方向盘交还给你。\n"
        "\n"
        "常规回合不必一张一张手动出牌 —— 用这个工具，一次调用打完整场。\n"
        "省下的调用次数就是省下的上下文。\n"
        "\n"
        "启发式只做**确定性的事**，照 docs/strategy.md 的实战结论：\n"
        "- 能斩杀就斩杀（杀掉一只怪 = 它今后每回合的输出都归零），且用 "
        "`damage_vs` 算实际伤害而非卡面值\n"
        "- 格挡是硬约束：先算够本回合要挡多少并满足它，剩余能量才用于输出\n"
        "- 敌人不攻击的回合一点格挡都不叠；场上有荆棘则先叠满格挡再攻击\n"
        "- Status / Curse 一律不打（可打出 ≠ 值得打）\n"
        "\n"
        "**返回值的 `stopped` 决定你接下来做什么**：\n"
        "- `combat_ended`：战斗打完了，照常处理奖励界面\n"
        "- `handoff`：**停手交还，需要你接管**。`reason` 说明为什么 —— "
        "血量过低、预计掉血超过血量 1/4、有致死风险、出现了待答的选择、"
        "或者手上有它不认识的牌。此时**一张牌都没有多打**，局面原样交给你，"
        "看 `state` 自己出牌。\n"
        "- `max_turns`：打满上限，局面仍在战斗中\n"
        "\n"
        "`log` 是它每一步的动作与理由，`state` 是停手时的局面"
        "（同样会写进决策日志，见 get_journal）。\n"
        "\n"
        "⚠️ 它**不会**用药水、不会应答选牌、不碰非战斗界面 —— 那些一律交还给你。"
    )
)
def auto_combat(max_turns: int = 20) -> dict[str, Any]:
    state = _request("GET", "/state")
    result = autoplay.play_combat(
        state,
        # 直接走 _request，不经 play_card / end_turn 那两个工具函数 ——
        # 它们各自也会记日志，那样每张牌会被记两遍
        play_card=lambda card, target: _request(
            "POST", "/action/play_card",
            {"card": card, **({"target": target} if target is not None else {})},
        ),
        end_turn=lambda: _request("POST", "/action/end_turn"),
        max_turns=max_turns,
        on_step=lambda before, label, why: journal.record(before, label, why, by="heuristic"),
    )
    # 停手的理由本身就是最该留下来的一条：它标出了启发式的边界在哪
    journal.record(result.get("state") or {},
                   f"auto_combat 停手（{result.get('stopped')}）",
                   result.get("reason") or "", by="heuristic", kind="stop")
    return result


@server.tool(
    description=(
        "读回**过去几局**的决策日志 —— 每条都带当时的层数、血量、做了什么、为什么。\n"
        "\n"
        "**开新局之前先看一眼**：上一局死在第几层、当时的构筑决策是什么、"
        "启发式在哪里停手交还。这些在上下文里早就没了，只有这份日志留得下来。\n"
        "\n"
        "为控制篇幅，战斗内由启发式逐张出的牌不会出现在摘要里"
        "（那是常规操作，价值低），保留的是：卡牌三选一、商店、除卡、路线、"
        "药水，以及每一次 `auto_combat` 停手交还的理由。\n"
        "\n"
        "`runs` 是往回看几局（默认 3，含当前这局），`per_run` 是每局最多几条。"
    )
)
def get_journal(runs: int = 3, per_run: int = 40) -> dict[str, Any]:
    return journal.digest(runs=runs, per_run=per_run)


@server.tool(
    description=(
        "检查桥接层是否就绪。连不上游戏时先用它定位问题。\n"
        "`attached` 为 false 表示桥接层没接入游戏帧循环，此时**动作一律无法下发**。"
    )
)
def health() -> dict[str, Any]:
    return _request("GET", "/health")


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
