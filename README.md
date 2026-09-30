# sts2-mcp

让程序自动游玩《杀戮尖塔 2》(Slay the Spire 2)。**策略写在代码里**，
不靠把攻略文档塞进模型上下文：出牌、选牌、路线、商店、休息点、事件都由
`src/agent/strategy/` 决定，模型（或命令行）只需要说一句「开始打」。

## 架构

```
Claude Code / 命令行 ──> agent (Python)  ──HTTP──> Sts2Bridge (游戏进程内 C#)
                          runner 循环                127.0.0.1:8765
                          strategy/ 决策                  ↑
                          data/ 静态数据     Sts2Profiler (CoreCLR Profiler, C++)
                                             IL 注入 NGame..cctor 加载桥接层
```

**设计原则：不修改游戏文件夹的任何一个字节。** 注入仅靠三个环境变量
（`CORECLR_ENABLE_PROFILING` / `CORECLR_PROFILER` / `CORECLR_PROFILER_PATH_64`），
Steam 更新与「验证文件完整性」都不会破坏本项目。

## 策略从哪来

| 来源 | 产物 | 用在哪 |
|---|---|---|
| `sts2.dll` 反编译（ilspycmd） | `data/cards.json` 等：费用、类型、目标、变量、升级改动，以及 **OnPlay 里的效果序列**（打几段、打谁、上什么能力、抽几张、生成什么牌） | 战斗模拟 |
| `SlayTheSpire2.pck` 本地化 | 中英文名与卡面文本 | 机制标签、日志 |
| [spire-codex.com](https://spire-codex.com/) 约 190 万局统计 | `data/metrics.json`：卡牌 Elo / 选取率 / 胜率 / 升级后胜率，遗物药水胜率，各角色主流构筑，各遭遇的平均掉血与致死率 | 选牌、升级、除卡、遗物、路线 |

刷新数据（游戏更新后）：

```powershell
python tools/extract_gamedata.py   # 需要 ilspycmd（dotnet tool install -g ilspycmd）
python tools/fetch_metrics.py
```

### 各部分怎么决定

- **战斗**（`strategy/combat.py`）：一回合内的束搜索。每张牌按抽出的效果结算，
  数字用 `/state` 里游戏算好的实时值（已含力量、易伤、升级）；回合末评估
  = −预计掉血 × 血量权重 + 有效伤害 + 击杀奖励 + 中毒/易伤/虚弱/能力的长期收益
  + 抽牌 − 药水成本。每打一张牌重新规划一次。
- **构筑**（`strategy/deck.py`）：卡牌估值 = 社区 Elo 与胜率（按样本量收缩、
  按章节调整）+ 与当前牌组的协同（社区构筑定义牌、卡面机制标签）+ 牌组缺口
  （AOE、前期输出）− 重复与费用曲线。低于门槛就跳过，门槛随牌组变大而升高。
- **路线**（`strategy/route.py`）：枚举本章全部路径，按社区统计的各类房间
  期望掉血推演血量，收益减风险取最优。
- **房间**（`strategy/rooms.py`）：奖励、三选一、休息点（回血与升级折成同一把尺子比）、
  商店（除卡 / 买牌 / 遗物 / 药水按性价比）、宝箱、事件（按渲染后的文本估得失）。
- **选牌应答**（`strategy/choices.py`）：按用途（弃牌 / 升级 / 获得）挑最差或最好的，
  用途来自桥接层导出的 `choice.purpose` / `choice.source`。

## 环境要求

| 依赖 | 版本 |
|---|---|
| Slay the Spire 2 | v0.107.1（Steam appid `2868840`） |
| .NET SDK | 9.x |
| VS Build Tools + C++ 工具集 | 编译 profiler 用 |
| Python | 3.12+ |

## 构建与运行

```powershell
# 1. 获取 CoreCLR profiling 头文件（Windows SDK 已不再包含）
.\scripts\fetch-headers.ps1

# 2. 编译 profiler 与桥接层
.\src\Sts2Profiler\build.ps1
dotnet build .\src\Sts2Bridge\Sts2Bridge.csproj -c Release

# 3. Python 依赖
python -m pip install -r src\agent\requirements.txt
```

然后在 Steam 中设置启动选项（右键游戏 → 属性 → 启动选项）：

```
"<仓库路径>\scripts\launch-steam.cmd" %command%
```

正常从 Steam 启动游戏即可，约 10 秒后桥接层就绪。改完桥接层后要重启游戏才能生效：
`.\scripts\restart-game.ps1`（会回退到最近的存档点）。

### 开始打

命令行：

```powershell
python scripts/play.py                  # 打到本局结束，逐步打印决策与理由
python scripts/play.py --advise         # 只看策略此刻会怎么做
python scripts/play.py --new-run silent # 一局结束后自动开下一局
```

Claude Code：仓库根目录的 `.mcp.json` 已注册 MCP server。

| 工具 | 说明 |
|---|---|
| `play(max_steps?, new_run_character?)` | 自动打到本局结束，返回层数、血量、牌组、遗物与最后 40 条决策 |
| `step()` | 按策略走一步 |
| `advise()` | 策略此刻会怎么做、为什么（不执行） |
| `get_state()` / `act(...)` / `health()` | 手动查看与干预 |

每局结束会在 `logs/runs.jsonl` 追加一行（角色、层数、牌组、遗物），用于评估改动。

测试：`python -m pytest tests`

## 桥接层接口

```
GET  /health                    存活状态
GET  /state                     游戏状态（含牌库 deck、整章地图 map.nodes）
GET  /glossary                  卡面文本字典（开发用）
GET  /describe?type=<类型全名>   列出类型的属性、字段与方法
GET  /eval?expr=<表达式>         即时求值只读表达式
GET  /tree?path=<节点路径>&depth=N  转储场景树（做界面时定位节点用）
POST /action/play_card?card=<手牌下标>[&target=<敌人下标>]
POST /action/end_turn
POST /action/use_potion?slot=<药水槽>[&target=<敌人下标>]
POST /action/choose?cards=<下标,下标…>        应答选牌
POST /action/move?node=<地图节点下标>         地图移动
POST /action/pick?i=<界面选项下标>            点击当前界面上的选项
POST /action/proceed                          按「继续」离开当前界面
POST /action/resume_run                       点主菜单「继续游戏」载入存档
```

- 下标与 `/state` 严格对应：`card` 对 `hand[].i`，`target` 对 `enemies[].i`。
- **动作是同步的**：等到局面稳定才返回，响应里附带执行后的新 `state`。
  结束回合要等完整个敌方回合，默认上限 20 秒（`?timeout=<毫秒>`）。
- 非法动作返回 **HTTP 200 + `ok:false`** 与结构化原因（`error` / `reason`），不是 4xx。
- **`awaiting_choice` 期间其他动作一律被拒**。带 `choice` 时用 `/action/choose` 应答；
  `choice.purpose` 是 CardSelectCmd 的入口名（`FromHandForDiscard` / `FromDeckForUpgrade` …），
  `choice.source` 是发起选择的牌或遗物。
- 界面选项（`screen.options`）：事件选项的 `id` 是跨语言稳定的 `TextKey`，带渲染后的
  `title` / `text` 与附带的 `relic`；卡牌三选一与遗物多选一带 `Alt:Skip` / `Alt:REROLL`。
- `map.nodes` 只在能移动时发送，每项为 `[行, 列, 类型, [[行, 列]…]]`。

## 配置

一律用环境变量。在 `scripts/launch-steam.cmd` 中设置（桥接层侧）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `STS2MCP_REPO` | 由脚本自动计算 | 仓库根目录。桥接层从临时副本加载，无法自行反推 |
| `STS2MCP_PORT` | `8765` | 桥接层 HTTP 端口 |
| `STS2MCP_ATTACH_FRAME` | 未设 | 设为 `1` 时接入 Godot 帧循环。下发动作的硬前置，启动脚本已默认开启 |
| `STS2MCP_CHOICE` | 未设 | 设为 `1` 时接管选牌。**会绕过选牌 UI**，无人应答时等 180 秒兜底 |
| `STS2MCP_BRIDGE_DLL` | 自动推导 | 覆盖桥接层 dll 路径，调试用 |

Python 侧：`STS2MCP_URL` 覆盖桥接层地址（默认 `http://127.0.0.1:8765`）。

## 目录结构

```
src/Sts2Profiler/    CoreCLR Profiler (C++)，负责 IL 注入
src/Sts2Bridge/      游戏内托管桥接层 (C#)：状态导出与动作执行
src/agent/           Python：bridge 客户端、runner 循环、MCP 入口
src/agent/strategy/  全部决策逻辑
src/agent/data/      生成的静态数据（勿手改，用 tools/ 重新生成）
tools/               数据抽取脚本
tests/               策略单测（构造的 /state，不需要游戏）
scripts/             构建、启动与辅助脚本
docs/game-model.md   游戏运行时数据结构与踩坑记录（维护桥接层用）
```

## 开发须知

- **桥接层不得编译期引用任何游戏侧程序集**，`GodotSharp` 也不行 ——
  编译期引用会导致程序集被重复加载，游戏在桥接层加载后约 10 毫秒硬崩溃。一律用反射。
- **`.cmd` / `.bat` 必须 CRLF**，`.ps1` 含中文时必须 **UTF-8 with BOM**。
- **不要用 `CardCmd.AutoPlay` 出牌**，正确路径是 `CardModel.TryManualPlay(target)`，
  详见 `docs/game-model.md`。
- 签名一律从反编译源码核对，不要照 `sts2.xml` 的注释推断。
- 重新编译 profiler 前须结束游戏进程。

## 边界

- **仅用于单机 / 离线游玩。** 在多人局中注入即作弊，本项目不支持、不用于多人模式。
- v0.107.1 为抢先体验版，游戏更新可能改变 `sts2.dll` 结构导致注入失效，
  也会让 `data/` 过期 —— 重新跑 `tools/` 即可。
