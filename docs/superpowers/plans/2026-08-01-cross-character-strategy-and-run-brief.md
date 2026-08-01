# 跨角色策略层 + 开局复盘（6.3b）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `docs/strategy.md` 重构成跨角色可用的判据层，并补上 spec.md 的 6.3b——用 `get_brief` / `set_plan` 两个工具把「上一局的事实」与「这一局的计划」接成闭环，再在 `auto_run` 上加一个开局停手点让它不靠自觉。

**Architecture:** 三段代码改动全部落在 `src/mcp_server/`：`journal.py` 加三个纯函数（`brief` / `has_plan` / `record_plan`），`server.py` 暴露两个 MCP 工具，`autorun.py` 的 `decide` 多接一个 `has_plan` 参数并多一条停手分支。文档改动落在 `docs/strategy.md`，是独立的一步。

**Tech Stack:** Python 3.11+、pytest、`mcp>=2.0`。不引入任何新依赖。

## Global Constraints

- 设计依据：`docs/superpowers/specs/2026-08-01-cross-character-strategy-and-run-brief-design.md`
- **日志是旁路，游戏才是主线**：`journal.py` 里新增的一切读写路径必须吞掉异常并降级，绝不能把一个已下发的动作变成失败回报（沿用该文件既有铁律）
- **失败方向必须是「放行」**：`has_plan` 读不到日志时返回 `True`（视同已写过计划），日志坏掉不许把 runner 卡在开局
- 测试用例名用中文，与 `test_journal.py` / `test_autorun.py` 现有风格一致
- 全量测试命令：`python -m pytest src/mcp_server -q`，改动前基线是 **89 passed**
- 注释与文档一律中文；`.ps1` 若有改动必须存为 UTF-8 with BOM（本计划不涉及）
- 不改 `autoplay.py` 的安全线判据，不改 `auto_combat`——那是地基

---

### Task 1: `journal.brief()` —— 抽出上一局的硬事实

**Files:**
- Modify: `src/mcp_server/journal.py`（在文件末尾的「读」区，`digest` 之后追加）
- Test: `src/mcp_server/test_journal.py`（在文件末尾追加）

**Interfaces:**
- Consumes: 同模块已有的 `_read_all()`、`_load_current()`
- Produces: `journal.brief() -> dict`，形状固定为
  `{"run": str|None, "character": str|None, "ended": bool, "floor": int|None, "act": int|None, "hp": int|None, "stops": {界面名: 次数}, "stop_reasons": [str], "builds": [{"floor": int, "action": str, "why": str}], "plan": str|None}`
  没有上一局时 `run` 为 `None`，其余字段取空值（`stops` 为 `{}`，列表为 `[]`）

- [ ] **Step 1: 写失败的测试**

追加到 `src/mcp_server/test_journal.py` 末尾：

```python
# --------------------------------------------------------------------------
#  开局复盘（spec.md 6.3b）
# --------------------------------------------------------------------------


def test_没有上一局时brief为空():
    assert journal.brief()["run"] is None


def test_只有菜单桶时视同没有上一局():
    journal.record({"in_run": False, "screen": {"type": "NMainMenu"}}, "pick 单人模式", "")
    assert journal.brief()["run"] is None


def test_brief取的是上一局而不是本局():
    journal.record(state(floor=9, character="Ironclad"), "上一局最后一步", "")
    journal.record(state(floor=1, character="Silent"), "本局第一步", "")
    b = journal.brief()
    assert b["character"] == "Ironclad"
    assert b["floor"] == 9


def test_brief给出死亡层数与死时血量():
    journal.record(state(floor=17, hp=11), "end_turn", "没牌可打了")
    journal.observe(state(floor=17, hp=0, game_over=True))
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    b = journal.brief()
    assert b["ended"] is True
    assert b["floor"] == 17 and b["hp"] == 0


def test_brief给出停手点分组与最后几条理由():
    combat = state(in_combat=True, combat={"turn": 4})
    journal.record(combat, "auto_combat 停手", "血量过低", by="heuristic", kind="stop")
    journal.record(state(screen={"type": "NMerchantRoom"}), "auto_run 停手", "商店要挑",
                   by="heuristic", kind="stop")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    b = journal.brief()
    assert b["stops"] == {"combat T4": 1, "NMerchantRoom": 1}
    assert "血量过低" in b["stop_reasons"]


def test_brief只收模型亲自做的构筑决策():
    journal.record(state(screen={"type": "NRewardsScreen"}), "pick 剑柄打击", "补输出")
    journal.record(state(in_combat=True, combat={"turn": 2}), "play_card 打击", "常规",
                   by="heuristic")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    b = journal.brief()
    assert [x["action"] for x in b["builds"]] == ["pick 剑柄打击"]


def test_brief带出上一局写下的计划():
    journal.record_plan(state(floor=1), "这一局优先拿降费件")
    journal.record(state(floor=5), "往下打", "")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    assert journal.brief()["plan"] == "这一局优先拿降费件"


def test_上一局没有终局记录时降级为见过的最大层数():
    journal.record(state(floor=3), "a", "")
    journal.record(state(floor=8), "b", "")
    journal.record(state(floor=1, character="Silent"), "新局第一步", "")
    b = journal.brief()
    assert b["ended"] is False and b["floor"] == 8


def test_brief读不到日志也不抛(monkeypatch):
    monkeypatch.setattr(journal, "PATH", "Z:/根本不存在的盘/decisions.jsonl")
    assert journal.brief()["run"] is None
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `python -m pytest src/mcp_server/test_journal.py -q -k brief`
Expected: FAIL，报 `AttributeError: module 'journal' has no attribute 'brief'`（以及 `record_plan` 同样不存在）

- [ ] **Step 3: 写最小实现**

在 `src/mcp_server/journal.py` 的 `digest()` 之后追加：

```python
def record_plan(state: dict[str, Any], plan: str) -> None:
    """记下本局的开局计划（spec.md 6.3b）。

    形状上就是一条普通决策，只是 `kind="plan"`。`digest` 的筛选规则是
    「带 kind 的一律留」，所以它天然会出现在下一局的 `brief()` 里 ——
    闭环不需要额外代码。
    """
    record(state, "本局计划", plan, by="model", kind="plan")


def has_plan(state: dict[str, Any]) -> bool:
    """本局是否已经写过开局计划。

    ⚠️ **失败方向是「放行」**：读不到日志、日志坏了，一律返回 True。
    这条判据只用来决定 runner 要不要停一次手，而日志是旁路 —— 让一次磁盘
    错误把整局卡在第 1 层，比漏做一次复盘糟得多。
    """
    try:
        rid = run_id(state)
        if rid == "menu":
            return True
        return any(e.get("run") == rid and e.get("kind") == "plan" for e in _read_all())
    except Exception:      # noqa: BLE001 —— 日志绝不能弄坏一步棋
        return True


# 上一局的「构筑决策」：模型亲自做的、且不在战斗内的那些。
# 判据与 digest 同源（by != heuristic），因为二者要的是同一批东西：
# 决定一局上限的地方（strategy.md §3）。
def _is_build(entry: dict[str, Any]) -> bool:
    return (entry.get("by") != "heuristic"
            and not str(entry.get("where", "")).startswith("combat")
            and entry.get("kind") != "plan")


def brief(runs_back: int = 1) -> dict[str, Any]:
    """上一局的硬事实。**不生成任何建议**（spec.md 6.3b）。

    「所以这一局该改什么」由模型自己写，再经 `record_plan` 记回日志 ——
    与 6.5 的结论一致：理由得由做决定的人写，模板拼出来的建议只会是套话。
    """
    empty: dict[str, Any] = {
        "run": None, "character": None, "ended": False, "floor": None,
        "act": None, "hp": None, "stops": {}, "stop_reasons": [],
        "builds": [], "plan": None,
    }
    try:
        entries = _read_all()
        current = str((_load_current() or {}).get("id") or "")

        order: list[str] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for e in entries:
            rid = str(e.get("run") or "?")
            if rid == "menu":
                continue          # 主菜单上的点击不属于任何一局
            if rid not in grouped:
                grouped[rid] = []
                order.append(rid)
            grouped[rid].append(e)

        previous = [rid for rid in order if rid != current]
        if not previous or runs_back > len(previous):
            return empty
        rid = previous[-runs_back]
        items = grouped[rid]

        end = next((e for e in items if e.get("kind") == "run_end"), None)
        floors = [e["floor"] for e in items if isinstance(e.get("floor"), int)]
        hps = [e["hp"] for e in items if isinstance(e.get("hp"), int)]

        stops: dict[str, int] = {}
        reasons: list[str] = []
        for e in items:
            if e.get("kind") != "stop":
                continue
            where = str(e.get("where") or "?")
            stops[where] = stops.get(where, 0) + 1
            if e.get("why"):
                reasons.append(str(e["why"]))

        plan = next((str(e.get("why") or "") for e in items if e.get("kind") == "plan"), None)

        return {
            "run": rid,
            # 角色名只存在于局标识里（`20260801-124500-Ironclad`），日志条目
            # 本身不记角色。这是本模块自己生成的格式，故可以拆；`#2` 后缀是
            # 同秒撞号时加的，要先去掉。
            "character": rid.split("#")[0].split("-")[-1] or None,
            "ended": end is not None,
            "floor": (end or {}).get("floor") or (max(floors) if floors else None),
            "act": (end or {}).get("act"),
            "hp": (end or {}).get("hp") if end else (hps[-1] if hps else None),
            "stops": stops,
            "stop_reasons": reasons[-3:],
            "builds": [
                {"floor": e.get("floor"), "action": e.get("action"), "why": e.get("why", "")}
                for e in items if _is_build(e)
            ],
            "plan": plan,
        }
    except Exception:      # noqa: BLE001 —— 复盘坏了不该影响下一步棋
        return empty
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest src/mcp_server/test_journal.py -q`
Expected: PASS，全部用例通过（原有 27 项 + 新增 9 项）

- [ ] **Step 5: 提交**

```bash
git add src/mcp_server/journal.py src/mcp_server/test_journal.py
git commit -m "6.3b 上一局的硬事实：brief / has_plan / record_plan"
```

---

### Task 2: 两个 MCP 工具 `get_brief` / `set_plan`

**Files:**
- Modify: `src/mcp_server/server.py`（紧跟在现有 `get_journal` 工具之后）

**Interfaces:**
- Consumes: Task 1 的 `journal.brief()`、`journal.record_plan(state, plan)`；同模块已有的 `_request("GET", "/state")`
- Produces: MCP 工具 `get_brief() -> dict`（直接返回 `journal.brief()` 的形状）与
  `set_plan(plan: str) -> dict`（返回 `{"ok": True, "plan": plan}`）

- [ ] **Step 1: 写实现**

在 `src/mcp_server/server.py` 中 `get_journal` 的定义之后追加：

```python
@server.tool(
    description=(
        "开新局时先调这个：把**上一局的硬事实**取回来。\n"
        "\n"
        "给的是事实，不是建议：上一局是哪个角色、结束在第几层第几幕、"
        "死时多少血、启发式在哪些界面停过手（含最后几条停手理由）、"
        "模型自己做过哪些构筑决策，以及上一局开局时写下的计划。\n"
        "\n"
        "**「所以这一局该改什么」要你自己写**，写完用 set_plan 记下来。"
        "上一局若是别的角色，构筑结论不能直接搬 —— 通则见 strategy.md，"
        "角色专属结论见该文 §6。"
    )
)
def get_brief() -> dict[str, Any]:
    return journal.brief()


@server.tool(
    description=(
        "写下这一局的开局计划（读完 get_brief 之后）。\n"
        "\n"
        "一两句话：这一局据上一局的哪个事实、要改什么。它会记进决策日志，"
        "下一局的 get_brief 读得到 —— 这样每一局的判断都接得上前一局。\n"
        "\n"
        "**auto_run 在第 1 层会因为「本局尚无计划」停手一次**，"
        "调过本工具之后它就不再因此停手。"
    )
)
def set_plan(plan: str) -> dict[str, Any]:
    journal.record_plan(_request("GET", "/state"), plan)
    return {"ok": True, "plan": plan}
```

- [ ] **Step 2: 确认模块能正常载入且工具已注册**

Run: `python -c "import sys; sys.path.insert(0, 'src/mcp_server'); import server; print(callable(server.get_brief), callable(server.set_plan))"`
Expected: 输出 `True True`

- [ ] **Step 3: 跑全量测试确认没碰坏别的**

Run: `python -m pytest src/mcp_server -q`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add src/mcp_server/server.py
git commit -m "6.3b 两个工具：get_brief 递事实，set_plan 记计划"
```

---

### Task 3: `auto_run` 的开局停手点

**Files:**
- Modify: `src/mcp_server/autorun.py:137-157`（`decide` 的签名与战斗分支之后）
- Modify: `src/mcp_server/autorun.py:237-272`（`play_run` 入口处求值一次并传下去）
- Test: `src/mcp_server/test_autorun.py`（末尾追加）

**Interfaces:**
- Consumes: Task 1 的 `journal.has_plan(state) -> bool`
- Produces: `decide(state, new_run_character=None, has_plan=True)`——**第三个参数默认 `True`**，即默认不因缺计划而停手；`play_run` 不新增参数，内部自行求值

- [ ] **Step 1: 写失败的测试**

追加到 `src/mcp_server/test_autorun.py` 末尾。沿用该文件已有的 `state()` 辅助函数
（`src/mcp_server/test_autorun.py:19`，签名是 `state(floor=5, hp=60, gold=99,
in_combat=False, screen=None, map_=None, **kw)`）：

```python
# --------------------------------------------------------------------------
#  开局先复盘（spec.md 6.3b）
# --------------------------------------------------------------------------


ONE_ROAD = {"can_move": True, "options": [{"i": 0, "type": "Monster"}]}


def test_第一层且本局无计划就停手():
    action, _, why = autorun.decide(state(floor=1, map_=ONE_ROAD), has_plan=False)
    assert action == "handoff"
    assert "复盘" in why


def test_第一层已有计划就照常走():
    action, _, _ = autorun.decide(state(floor=1, map_=ONE_ROAD), has_plan=True)
    assert action == "move"


def test_默认不因缺计划停手():
    """不传 has_plan 时行为必须与从前完全一致 —— 老调用方一个都不能被绊住。"""
    action, _, _ = autorun.decide(state(floor=1, map_=ONE_ROAD))
    assert action == "move"


def test_半局接手不被复盘这条拦住():
    """第 5 层才开 runner 是常事，那时补写开局计划已无意义。"""
    action, _, _ = autorun.decide(state(floor=5, map_=ONE_ROAD), has_plan=False)
    assert action == "move"


def test_战斗中不因缺计划打断():
    """第 1 层直接开打时，停手要等这场打完 —— 半场撂挑子比漏一次复盘更糟。"""
    action, _, _ = autorun.decide(state(floor=1, in_combat=True), has_plan=False)
    assert action == "combat"
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `python -m pytest src/mcp_server/test_autorun.py -q -k "计划 or 复盘"`
Expected: FAIL，报 `TypeError: decide() got an unexpected keyword argument 'has_plan'`

- [ ] **Step 3: 改 `decide` 的签名与分支**

把 `src/mcp_server/autorun.py` 的 `decide` 签名与开头改成：

```python
def decide(state: dict[str, Any], new_run_character: str | None = None,
           has_plan: bool = True) -> tuple[str, Any, str]:
    """下一步做什么。返回 `(动作, 参数, 理由)`。

    动作为 `handoff` 时，参数即交还的理由。**顺序即优先级**，越靠前越硬。

    `has_plan` 为 False 时，第 1 层会多一个停手点：先复盘上一局（6.3b）。
    默认 True —— 调用方不关心这件事时，行为与从前完全一致。
    """
```

然后在**战斗分支之后**（现有 `if state.get("in_combat"): return "combat", ...` 的下一行）插入：

```python
    # 开局先复盘上一局（spec.md 6.3b）。放在战斗分支**之后**是有意的：
    # 第 1 层直接开打时，半场撂挑子比漏一次复盘更糟，等这场打完再停。
    # 6.3b 拖了这么久没做，正因为它全靠自觉；而本项目治「靠自觉」的办法
    # 一向是：该做决策的地方让 runner 停下来。
    floor = (state.get("run") or {}).get("total_floor")
    if not has_plan and isinstance(floor, int) and floor <= 1:
        return "handoff", None, (
            "新局开始且本局尚无开局计划 —— 先 get_brief 复盘上一局，"
            "再 set_plan 写下这一局要改什么（spec.md 6.3b）"
        )
```

- [ ] **Step 4: 让 `play_run` 求值并传下去**

在 `play_run` 函数体内 `last_sig = None` 之后追加一行：

```python
    # 一次 play_run 调用期间这个值不会变（set_plan 只可能发生在两次调用之间），
    # 故只求值一次，不必每步都读日志。
    has_plan = journal.has_plan(state)
```

并把循环里的 `decide` 调用改成：

```python
        action, arg, why = decide(state, new_run_character=new_run_character,
                                  has_plan=has_plan)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest src/mcp_server -q`
Expected: PASS，全部通过（基线 89 项 + Task 1 的 9 项 + 本任务 5 项）

- [ ] **Step 6: 提交**

```bash
git add src/mcp_server/autorun.py src/mcp_server/test_autorun.py
git commit -m "6.3b auto_run 开局停手：没复盘上一局就不往下走"
```

---

### Task 4: `strategy.md` 跨角色重构

**Files:**
- Modify: `docs/strategy.md`（整体重排，§3 重写）

**Interfaces:**
- Consumes: 无代码依赖
- Produces: 无代码接口。产出是一份新结构的文档，供模型开局与做构筑决策时查

- [ ] **Step 1: 按目标骨架重排**

目标骨架（详见设计文档 §3.1）：

```
§0  开局：先复盘上一局              新增，指向 get_brief / set_plan
§1  战斗 · 出牌                     原 §1 十条，判据一字不改，只换例子
§2  战斗 · 什么时候必须停手         原 §3 整节搬移
§3  构筑：决定一局上限              重写，见 Step 2
§4  路线与资源                      原 §2
§5  算伤害用 values，不用卡面       原 §4，不动
§6  角色档案                        新增
§7  待验证                          原 §7
```

开篇铁律那段保留，并补一句：

```markdown
本文正文是**跨角色通则**：判据不含角色，牌名一律只作括号里的例子。
角色专属的结论一概进 §6 角色档案 —— 换角色时，§1–§5 照用，§6 换一节读。
```

- [ ] **Step 2: 重写 §3（唯一真正重写的一节）**

`§3.1` 写成：

```markdown
### 3.1 构筑优先级：引擎件 > 降费/回能件 > 单卡伤害数字

- **引擎件**：每回合自动产出、且产出随回合数增长的东西
  （静默：涂毒——每次未被格挡的攻击叠中毒，升级后 2 层）
- **降费/回能件**：把「一回合能打出几张牌」抬上去的东西
  （力士：放血 0 费回 3 能、战鼓、遗忘仪式）
- **单卡伤害**：只提高单次投送量的攻击牌

**判据**：三局都倒在同一个算式上 —— 每回合约 14 点、Boss 222 血需要约 15
回合，而我方只撑得住约 8 回合。**单卡伤害提高的是分子；引擎件与降费件改变的
是算式本身。**

依赖：`screen.options[].title` / `.text`（候选物的效果文本，6.4c）。
```

`§3.2`–`§3.7` 每条的**必须包含的判据**与**证据出处**如下（证据一律从现有
`docs/strategy.md` 原样搬，不重写数字）：

| 小节 | 必须写进去的判据 | 证据出处（现有 strategy.md） |
|---|---|---|
| 3.2 引擎件 | 怎么认：产出随回合数增长、且不占每回合出牌位；何时铺：来袭伤害低于当前格挡的回合是零代价窗口 | 无历史证据，标注「2026-08-01 静默局首次采用，验证中」 |
| 3.3 降费/回能件 | 怎么认：它改变的是「一回合能打几张牌」，不是单张牌的数字 | §5「2026-08-01 再复核：能量优先的一局终于打过」整段 |
| 3.4 缩表 | 除卡优先除起手最弱的输出牌；**不拿牌也是一种拿**，牌组越薄关键牌抽到得越勤 | 原 §2.3 整条 + §6 里「75 金除掉一张打击」那句 |
| 3.5 升级选谁 | 升引擎件优先于升单卡伤害；升级效果**必须打出来核对**，不能照一代的印象假定（涂毒升级实测是 2 层中毒而非降费） | 原 §2.5 打铁那段 + 2026-08-01 静默局的实测 |
| 3.6 商店买什么 | 先问「这一局到目前为止是被什么卡住的」，再看商品 | 原 §6 整节，**原样保留**「就这一次的数据，不当通则」这句 |
| 3.7 休息点 | 按「这一局会死于什么」选：怕被磨死就烤火，怕打不动就打铁 | 原 §2.5 整节 |

3.6 与 3.7 的数据量仍只有一次，措辞不得升级成通则。

- [ ] **Step 3: 把血的教训挂到对应通则下**

四段证据一字不改地搬到对应位置（设计文档 §3.3）：

| 证据 | 归到 |
|---|---|
| v1 启发式在 HP 37/70 面对四怪时全灭 | §2 |
| 第一章 Boss 战差 5 点没跨过击晕线 | §5 |
| 两局分别差 16 / 33 血倒在 Boss | §3.1 |
| 能量优先的第三局第一次打过 Boss | §3.1 |

- [ ] **Step 4: 写 §6 角色档案**

```markdown
## 6. 角色档案

只写**本角色已实战验证**的内容。没打过的角色不预先臆测 —— 一代的经验不能
假定二代照搬（Boss 遗物三选一那次就是这么错的，见 spec.md §3.4g）。

### 6.1 力士 Ironclad（已验证，3 局）
核心件：放血（0 费回 3 能）、战鼓、遗忘仪式、破灭；辅以战斗冥想补牌。
路线：能量优先，第三局据此第一次打过第一章 Boss（222 血）。
死法：前两局输出不足，分别差 16 / 33 血倒在第一章 Boss。

### 6.2 静默猎手 Silent（验证中）
已验证：涂毒（升级后每次未格挡攻击叠 2 层）+ 刀刃之舞（3 张小刀 = 3 次攻击）
是相乘关系。2026-08-01 开局据 §3.1 选下这两张，尚未打到 Boss。

### 6.3 其他角色
`autorun.py` 的 `_CHARACTER_ALIASES` 另列了故障机器人、亡灵契约师、摄政王
三个，但**该表没有注明来源，尚未与角色选择界面核对过**。核对之前不在这里
建节 —— 名单本身还不是事实。
```

- [ ] **Step 5: 验收检查**

Run: `python -m pytest src/mcp_server -q`（确认没碰到代码）
Expected: PASS

人工核对三条：
1. §1–§5 的判据里没有任何一条**只有**某个角色才成立却未标注
2. 四段血的教训都还在，数字一个没丢
3. §6 只有力士与静默两节

- [ ] **Step 6: 提交**

```bash
git add docs/strategy.md
git commit -m "策略：抽出跨角色通则，角色专属结论收进角色档案"
```

---

### Task 5: 更新 spec.md 的 6.3b 与结论

**Files:**
- Modify: `docs/spec.md:1054-1056`（6.3b 打勾）
- Modify: `docs/spec.md`（阶段 6 的结论区追加一段「6.3b 结论」）

**Interfaces:**
- Consumes: Task 1–4 的成果
- Produces: 无代码接口

- [ ] **Step 1: 把 6.3b 从 `- [ ]` 改成 `- [x]`**

原文：

```markdown
- [ ] 6.3b **开新局前复盘上一局**：先读 `get_journal`，把上一局死亡楼层、
      构筑取舍与停手原因压成这一局的短提示。暂不把整份 strategy.md 再复制成
      一套 prompt 模板——它会很快与原文漂移，当前增量价值也低
```

改为：

```markdown
- [x] 6.3b **开新局前复盘上一局** → `get_brief`（只递上一局的硬事实）+
      `set_plan`（只记模型自己写的教训），并在 `auto_run` 上加了开局停手点。
      仍不把 strategy.md 复制成 prompt 模板——它会很快与原文漂移。
      见下方「6.3b 结论」
```

- [ ] **Step 2: 在阶段 6 的结论区追加**

```markdown
#### 6.3b 结论：靠自觉的事情，就得让 runner 停一次手

这条从 6.3 拆出来之后挂了整整一天没做，而它的实现量只有一个下午。原因不是
难，是**每次开新局都急着往下打**——日志积了 781 条、5 局，一次都没在开局时
被读过。

所以实现里最要紧的不是 `brief()` 抽了哪几个字段，而是 `auto_run` 那个停手点：
第 1 层且本局尚无 plan 就交还。这与 6.4 的整体原则同源——**该做决策的地方让
runner 停下来**，只不过这次停的是「还没想清楚这一局要改什么」。

两处刻意的取舍：

- **停手点放在战斗分支之后**。第 1 层直接开打时，半场撂挑子比漏一次复盘更糟。
- **`has_plan` 读不到日志时返回 True**（放行）。日志是旁路，让一次磁盘错误把
  整局卡在第 1 层，比漏做一次复盘糟得多。

以及沿用 6.5 的结论：`brief()` **不生成任何建议**。「所以这一局该改什么」由
模型自己写，经 `set_plan` 记回日志，下一局的 `brief()` 再读到它 —— 闭环靠
`digest` 既有的「带 kind 的一律留」白拿，没写一行新代码。
```

- [ ] **Step 3: 提交**

```bash
git add docs/spec.md
git commit -m "6.3b 打勾：开局复盘接成闭环"
```

---

## 验收

全部任务完成后：

```bash
python -m pytest src/mcp_server -q      # 期望 103 passed（89 + 9 + 5）
git -C . status --short                 # 期望干净
```

实机验收（下次开新局时）：`auto_run` 应在第 1 层停手一次，理由含「先 get_brief
复盘上一局」；调过 `set_plan` 之后再跑，不再因此停手。
