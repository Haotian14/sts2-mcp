"""`set_plan` 工具的回归测试（Important 1）。

【测什么】
`set_plan` 曾经无条件 `return {"ok": True, "plan": plan}` —— 哪怕
`journal.record_plan` 因为磁盘写不进去而没能真正落盘，模型依然被告知
「记下来了」。下一次 `auto_run` 在第 1 层发现日志里根本没有 plan 条目，
又停手要求复盘；模型（相信自己已经写过）会一脸茫然地再调一次 set_plan，
如此循环。这里钉住：写失败必须体现在返回值的 `ok` 上。

`_request` 会打真实 HTTP 到游戏内桥接层，测试环境没有桥接层可连，故一律
打桩绕过网络；日志文件位置隔离到 tmp_path，不碰仓库里真实的 logs/。

运行：python -m pytest src/mcp_server/test_server.py -q
"""

from __future__ import annotations

import journal
import server
from conftest import _block_append


def _fake_state() -> dict:
    return {
        "in_run": True,
        "run": {"act": 1, "total_floor": 1, "ascension": 2, "gold": 0, "game_over": False},
        "player": {"character": "Ironclad", "hp": 70, "max_hp": 80},
    }


def test_写入成功时如实回报ok为True(monkeypatch):
    monkeypatch.setattr(server, "_request", lambda method, path, params=None: _fake_state())
    result = server.set_plan("这一局先补格挡")
    assert result == {"ok": True, "plan": "这一局先补格挡"}
    # 真的落了盘——下一局的 get_brief 读得到
    assert journal.has_plan(_fake_state()) is True


def test_写不进去时不能谎报ok为True(monkeypatch):
    """核心场景：日志可读（哪怕还没有任何内容），但这一次写入会失败。
    修复前这里会得到 `{"ok": True, ...}`——模型以为计划记下来了，
    实际上什么都没发生。"""
    monkeypatch.setattr(server, "_request", lambda method, path, params=None: _fake_state())
    _block_append(monkeypatch)

    result = server.set_plan("这一局先补格挡")
    assert result["ok"] is False
    assert result["plan"] == "这一局先补格挡"        # 模型写的内容原样退回，方便它自己记住
    assert "error" in result and result["error"]     # 必须说明白，不能只给个 False


def test_日志写不进去时动作工具依然回报ok为True(monkeypatch):
    """`server._log()` 明确丢弃 `journal.record()` 的返回值——日志是旁路，
    游戏才是主线（journal.py 的铁律）。但此前没有任何用例钉住这条原则：
    谁把 `_log` 悄悄改成 `if not journal.record(...): return {"ok": False}`，
    全部既有用例照样全绿，因为它们要么打桩绕开了真实写盘，要么根本没检查
    写失败时动作本身的返回值。这里让日志写入真的失败（磁盘只读/满/ACL），
    钉住 `pick` 这类动作工具的 `ok` 只应体现游戏侧结果，与日志有没有落盘
    无关。"""
    fake_game_result = {"ok": True, "state": _fake_state()}
    monkeypatch.setattr(server, "_request", lambda method, path, params=None: fake_game_result)
    _block_append(monkeypatch)

    result = server.pick(0, why="随便挑一个")
    assert result["ok"] is True
