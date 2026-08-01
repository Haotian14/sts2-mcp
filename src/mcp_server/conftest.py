"""本目录全部测试共用的 fixture。

【为什么要有这个文件】
「测试不得读写仓库里真实的 `logs/`」是硬约束，此前全靠每个测试文件自觉
——`test_journal.py` / `test_autorun.py` / `test_server.py` 各自抄了一份
「把 journal.PATH / journal._RUN_FILE 指到 tmp_path」的 fixture。风险不在
重复本身，而在于**下一个新增的测试文件默认不受保护**：谁忘了抄这一份，
它的用例就会真的往仓库的 `logs/decisions.jsonl` 里写东西。

放进 `conftest.py` 并标 `autouse=True`，对本目录下所有测试文件默认生效，
新增文件不需要再自己操心。
"""

from __future__ import annotations

import pytest

import journal


@pytest.fixture(autouse=True)
def 隔离日志(tmp_path, monkeypatch):
    """把决策日志与「当前局」标识都指到 tmp_path，不碰仓库里真实的 logs/。

    只隔离路径，不改变任何行为——尤其是 `has_plan` 之类的真实逻辑仍然按
    真实实现跑，不在这里打桩（`test_autorun.py` 原有的同名 fixture专门
    强调过这一点：它不再把 `has_plan` 桩死成 True，否则所有经过 `play_run`
    的用例都会在「开局复盘」这个特性关闭的假状态下跑，往后任何新用例都会
    默默继承这个假状态）。
    """
    monkeypatch.setattr(journal, "PATH", str(tmp_path / "decisions.jsonl"))
    monkeypatch.setattr(journal, "_RUN_FILE", str(tmp_path / "current-run.json"))
    monkeypatch.setattr(journal, "_warned", False)
    return tmp_path


def _block_append(monkeypatch):
    """把 `open(PATH, "a", ...)` 变成必现 `OSError`，模拟磁盘只读 / 满 /
    ACL 限制，同时不影响默认的 "r" 模式读 —— 这是「日志可读、却写不进去」
    这一故障模式的最小复现（不依赖 chmod 在部分账户/平台下不生效的坑）。
    """
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if str(path) == journal.PATH and "a" in mode:
            raise OSError("模拟磁盘写入失败")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
