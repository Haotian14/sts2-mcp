"""直接调用 MCP server 里的某个工具函数 —— 开发期验证用，不经 MCP 协议。

【为什么需要它】
与 `autocombat.py` 同一个理由：MCP 工具列表（含每个工具的参数）在会话启动时
就固定了，改完 `server.py` 得重启整个 Claude Code 会话才看得见新参数。而
「写完不等于能用」是本项目的第一条规矩 —— 新代码必须当场在真游戏上验一遍。
本脚本在**另一个进程**里 import 同一份 `server.py`，走的是同样的代码路径
（含决策日志），只是绕过了 MCP 那层壳。

用法：
    python scripts/call.py get_state
    python scripts/call.py pick 0 why="金币永远该拿"
    python scripts/call.py move 1 why="血够，走精英换遗物"
    python scripts/call.py choose cards=0,2 why="除掉两张打击"

注意：每次调用都是一个新进程，而 `server.py` 用 `_last_state` 缓存「出手前的
局面」来给决策日志起名字。故本脚本会先替你调一次 `get_state` 把它填上 ——
真跑在 MCP 里时这一步不需要，因为上一个动作的返回值就是它。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "mcp_server"))
import server  # noqa: E402


# 收列表的参数：`cards=0` 只有一个元素时没有逗号可认，会被当成整数传下去，
# 而工具那边 `for c in cards` 直接炸。按参数名判定，不靠值的形状猜。
LIST_ARGS = ("cards",)


def _value(raw: str, key: str = ""):
    if key in LIST_ARGS:
        return [int(p) for p in raw.split(",") if p.strip()]
    if raw.lstrip("-").isdigit():
        return int(raw)
    if "," in raw:
        parts = raw.split(",")
        if all(p.strip().lstrip("-").isdigit() for p in parts if p.strip()):
            return [int(p) for p in parts if p.strip()]
    return raw


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    name = sys.argv[1]
    fn = getattr(server, name, None)
    if not callable(fn):
        print(f"没有这个工具：{name}")
        return 2

    args, kwargs = [], {}
    for raw in sys.argv[2:]:
        if "=" in raw and not raw.split("=", 1)[0].lstrip("-").isdigit():
            k, v = raw.split("=", 1)
            kwargs[k] = _value(v, k)
        else:
            args.append(_value(raw))

    if name != "get_state":
        server.get_state()      # 填上 _last_state，好让日志记得出名字

    print(json.dumps(fn(*args, **kwargs), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
