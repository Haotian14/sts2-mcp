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

import pytest

import journal
import server


@pytest.fixture(autouse=True)
def 隔离日志(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "PATH", str(tmp_path / "decisions.jsonl"))
    monkeypatch.setattr(journal, "_RUN_FILE", str(tmp_path / "current-run.json"))
    monkeypatch.setattr(journal, "_warned", False)


def _fake_state() -> dict:
    return {
        "in_run": True,
        "run": {"act": 1, "total_floor": 1, "ascension": 2, "gold": 0, "game_over": False},
        "player": {"character": "Ironclad", "hp": 70, "max_hp": 80},
    }


def _block_append(monkeypatch):
    """把追加写变成必现 OSError，模拟磁盘只读/满/ACL 限制——与
    test_journal.py 里的同名手法一致，这里独立写一份是因为两个测试文件
    互不导入对方的私有辅助函数。"""
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if str(path) == journal.PATH and "a" in mode:
            raise OSError("模拟磁盘写入失败")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)


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
