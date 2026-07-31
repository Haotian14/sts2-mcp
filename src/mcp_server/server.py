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
        "- `in_combat`：是否在战斗中。为 false 时只有 run / player / relics / potions 有内容。\n"
        "- `awaiting_choice`：**为 true 时游戏正等玩家做选择**（弃哪张牌、检索哪张、"
        "三选一拿哪个）。此时任何动作都会被游戏取消，不要下发 —— "
        "应答选择的接口尚未实现，需要人来点。\n"
        "- `hand[].i`：出牌用的下标。**每打出一张牌，剩余手牌的下标就会重排**，"
        "所以连续出牌时每次都要以最新状态为准，不能沿用旧下标。\n"
        "- `hand[].playable`：能否打出；为 false 时 `reason` 给出原因"
        "（EnergyCostTooHigh / HasUnplayableKeyword 等）。\n"
        "- `hand[].target`：目标类型。**只有 AnyEnemy / AnyAlly 需要指定目标**，"
        "Self / AllEnemies 之类传了目标反而非法。\n"
        "- `hand[].cost`：能量消耗；为 null 表示这张牌不可打出（诅咒牌）。\n"
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
        "\n" + _ACTION_RESULT_DOC
    )
)
def play_card(card: int, target: int | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"card": card}
    if target is not None:
        params["target"] = target
    return _request("POST", "/action/play_card", params)


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
def end_turn() -> dict[str, Any]:
    return _request("POST", "/action/end_turn")


@server.tool(
    description=(
        "使用一瓶药水。\n"
        "\n"
        "`slot` 是 get_state 里 `potions` 数组的下标，该数组保留空槽（null），"
        "所以下标即槽位号。`target` 取自 `enemies[].i`，仅对指定单体敌人的药水需要；"
        "作用于自己的药水省略即可。\n"
        "\n"
        "药水在战斗外也能喝（比如上路前先回血）。\n"
        "\n" + _ACTION_RESULT_DOC
    )
)
def use_potion(slot: int, target: int | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"slot": slot}
    if target is not None:
        params["target"] = target
    return _request("POST", "/action/use_potion", params)


# --------------------------------------------------------------------------


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
