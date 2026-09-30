"""游戏内桥接层（Sts2Bridge，127.0.0.1:8765）的 HTTP 客户端。

动作接口是同步的：桥接层等到局面稳定才返回，响应里带执行后的 `state`。
非法动作是 HTTP 200 + `ok:false`（正常的游戏结果）；4xx/连不上才是 BridgeError。
"""

from __future__ import annotations

import os
from typing import Any

import httpx

BRIDGE_URL = os.environ.get("STS2MCP_URL", "http://127.0.0.1:8765").rstrip("/")

# 读超时必须大于桥接层等待动作稳定的上限（20 秒）：end_turn 要等完整个敌方回合。
# 若这里先超时，动作其实已经下发，调用方却以为失败了。
_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)


class BridgeError(RuntimeError):
    """桥接层不可达或返回了协议级错误。"""


class Bridge:
    def __init__(self, url: str = BRIDGE_URL) -> None:
        self.url = url
        self._client = httpx.Client(timeout=_TIMEOUT, trust_env=False)   # 桥接层在本机，不走系统代理

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            r = self._client.request(method, f"{self.url}{path}", params=params)
        except httpx.ConnectError as exc:
            raise BridgeError(f"连不上桥接层 {self.url}：游戏是否在运行、启动选项是否设置了 "
                              f"scripts/launch-steam.cmd？（{exc}）") from exc
        except httpx.ReadTimeout as exc:
            raise BridgeError(f"桥接层超时（{path}）。动作可能已下发，先读一次状态再决定，不要重发。") from exc
        try:
            payload = r.json()
        except ValueError as exc:
            raise BridgeError(f"桥接层返回的不是 JSON（HTTP {r.status_code}）：{r.text[:200]}") from exc
        if r.status_code >= 400:
            raise BridgeError(f"桥接层拒绝了请求（HTTP {r.status_code}）：{payload}")
        return payload

    def state(self) -> dict[str, Any]:
        return self._request("GET", "/state")

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def act(self, action: str, **params: Any) -> dict[str, Any]:
        """POST /action/<action>。参数为 None 的不发。"""
        clean = {k: v for k, v in params.items() if v is not None}
        if isinstance(clean.get("cards"), list):
            clean["cards"] = ",".join(str(c) for c in clean["cards"])
        return self._request("POST", f"/action/{action}", clean)
