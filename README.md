# sts2-mcp

让 Claude 通过 MCP 自动游玩《杀戮尖塔 2》(Slay the Spire 2)。

> **状态（2026-07-30）：注入链路已打通，游戏状态可实时读取。**
> 技术未知已全部消除，余下为确定性工程量。详见 `docs/spec.md`。

## 这是什么

一个游戏内 C# 桥接层 + 一个 Python MCP server，让 Claude 能够：

- 读取精确的游戏状态（手牌、能量、怪物意图、遗物、地图……）
- 执行游戏动作（出牌、结束回合、选牌、走地图、商店、事件）
- 自主爬塔

## 架构

```
Claude Code ──MCP(stdio)──> MCP Server (Python) ──HTTP──> Sts2Bridge (游戏进程内 C#)
  决策                      工具契约 / 自动驾驶循环         127.0.0.1:8765
                                                              ↑
                                              Sts2Profiler (CoreCLR Profiler, C++)
                                              IL 注入 NGame..cctor 加载桥接层
```

**设计原则：不修改游戏文件夹的任何一个字节。** 注入仅靠三个环境变量
（`CORECLR_ENABLE_PROFILING` / `CORECLR_PROFILER` / `CORECLR_PROFILER_PATH_64`），
Steam 更新与「验证文件完整性」都不会破坏本项目。

## 进度

| 阶段 | 状态 |
|---|---|
| 1 注入链路（Profiler → IL 注入 → 托管桥接层） | ✅ |
| 2 状态导出 | ✅ 战斗内（`/state` + `/glossary`）；2.4 非战斗场景待实现 |
| 3 动作执行 | ✅ 战斗内（出牌/结束回合/药水/选牌）已实机验证；3.4b 非战斗待实现 |
| 4 传输层（进程内 HTTP） | ✅ |
| 5 Python MCP server | ✅ 工具已可用；5.4 自动驾驶循环待 3.4 就绪后再做 |
| 6 决策策略与自动驾驶 | ⬜ |

## 为什么可行

《杀戮尖塔 2》是 Godot 4 + C#/.NET 9，且：

- `sts2.dll` **完全未混淆**（`CombatManager`、`Monster`、`Relic` 等均为明文）
- 随游戏附带 `sts2.xml` —— **5.2 MB 官方 API 文档注释**，命名空间 `MegaCrit.Sts2.*`
- 游戏内部存在与 UI 解耦的 **GameAction 队列**，玩家出牌即经由
  `PlayCardAction` 入队执行，可程序化驱动
- 多人模式迫使游戏把玩家决策抽象为可注入的 `PlayerChoiceContext`，
  并提供了为序列化设计的 `NetFullCombatState`

## 环境要求

| 依赖 | 版本 |
|---|---|
| Slay the Spire 2 | v0.107.1（Steam appid `2868840`） |
| .NET SDK | 9.x |
| VS Build Tools + C++ 工具集 | 编译 profiler 用 |
| Python | 3.12+（阶段 5 起） |

## 构建与运行

```powershell
# 1. 获取 CoreCLR profiling 头文件（Windows SDK 已不再包含）
.\scripts\fetch-headers.ps1

# 2. 编译 profiler 与桥接层
.\src\Sts2Profiler\build.ps1
dotnet build .\src\Sts2Bridge\Sts2Bridge.csproj -c Release
```

然后在 Steam 中设置启动选项（右键游戏 → 属性 → 启动选项）：

```
"<仓库路径>\scripts\launch-steam.cmd" %command%
```

正常从 Steam 启动游戏即可。约 10 秒后桥接层就绪，可访问：

```
GET  /health                    存活状态
GET  /state                     游戏状态（动态数据，约 1.5 KB）
GET  /glossary                  卡面文本字典（静态数据，一局取一次）
GET  /describe?type=<类型全名>   列出类型的属性、字段与方法
GET  /eval?expr=<表达式>         即时求值只读表达式
POST /action/play_card?card=<手牌下标>[&target=<敌人下标>]
POST /action/end_turn
POST /action/use_potion?slot=<药水槽>[&target=<敌人下标>]
POST /action/choose?cards=<下标,下标…>        应答选牌（弃牌/检索/留牌）
```

动作接口（`POST`，读接口一律 `GET`）：

```powershell
Invoke-RestMethod -Method Post 'http://127.0.0.1:8765/action/play_card?card=0&target=1'
```

下标与 `/state` 严格对应：`card` 对 `hand[].i`，`target` 对 `enemies[].i`。
响应里默认附带一份执行后的新 `/state`（`?state=0` 可关掉），省掉一次往返：

```json
{"ok":true,"action":"play_card","card":"StrikeSilent","target":"CorpseSlug",
 "settled":true,"waited_ms":312,"state":{ ... }}
```

- **动作是同步的**：请求会一直等到局面稳定（队列排空、效果跑完、回合阶段
  回到 `Play`）才返回。结束回合要等完整个敌方回合，默认上限 20 秒，
  可用 `?timeout=<毫秒>` 调整。客户端超时须设得比它更长。
- `settled:false` 表示局面未稳定，随附状态可能是中间态，应重新拉 `/state`
  再做决策。其中一种情形会额外带上 `awaiting_choice:true` 与 `screen`：
  动作停下来等玩家做选择了（如「求生者」要弃一张牌），须由上层应答该界面
  —— 这属于阶段 3.4，尚未实现。
- 非法动作返回 **HTTP 200 + `ok:false`** 与结构化原因，不是 4xx ——
  「这步不能走」和「桥接层坏了」必须能分辨：

```json
{"ok":false,"action":"play_card","error":"unplayable",
 "reason":"EnergyCostTooHigh","detail":"Backflip 现在打不出","state":{ ... }}
```

  `error` 取值：`not_attached` / `not_in_combat` / `not_ready` /
  `actions_disabled` / `bad_index` / `bad_target` / `unplayable` /
  `empty_slot` / `already_queued` / `awaiting_choice` / `rejected`。
- **`awaiting_choice` 期间其他动作一律被拒**（游戏会把入队的出牌取消掉）。
  同时带 `choice` 字段时用 `/action/choose` 应答；没有 `choice` 说明是桥接层
  还接管不了的选择（地图、商店、事件），只能由人操作。

`/state` 与 `/glossary` 的**动静分离**是刻意的：卡面文本每回合一字不变，
若与状态合并则每个决策点都要重传一遍，token 成本随回合数线性增长。
MCP server 应缓存 `/glossary`，只反复拉取 `/state`。

`/eval` 是开发期的核心工具 —— 可在游戏运行时探查任意对象，
无须改代码重启。例：

```
/eval?expr=MegaCrit.Sts2.Core.Combat.CombatManager::Instance._state.Enemies[0]
```

不想启用时，把 Steam 启动选项清空即可，游戏恢复原样。

### 在 Claude Code 里玩

```powershell
python -m pip install -r src\mcp_server\requirements.txt
```

仓库根目录的 `.mcp.json` 已注册好 MCP server，在本仓库里启动 Claude Code
即可（首次会提示批准）。工具：

| 工具 | 说明 |
|---|---|
| `get_state` | 当前局面，一切决策的依据 |
| `get_glossary` | 卡面文本，一局取一次（server 侧缓存） |
| `play_card(card, target?)` | 下标取自 `get_state`；**只有 AnyEnemy/AnyAlly 的牌才传 target** |
| `end_turn()` | 等敌方回合走完才返回，5~8 秒属正常 |
| `use_potion(slot, target?)` | 战斗外也可用 |
| `choose(cards)` | 应答选牌（弃牌/检索/留牌），选项见 `get_state` 的 `choice` |
| `health()` | 连不上游戏时先用它定位 |

动作工具会自行等到局面稳定，并在返回值里附带执行后的新状态 ——
**不需要在动作之后再调一次 `get_state`**。

`STS2MCP_URL` 可覆盖桥接层地址（默认 `http://127.0.0.1:8765`）。

### 配置

本项目一律用环境变量配置，不用配置文件 —— 桥接层运行在游戏进程内、启动时序
极早，读文件需处理路径解析与失败回退；而环境变量本就要由启动脚本注入
（profiler 那三个就是），顺带多传几个零成本。

在 `scripts/launch-steam.cmd` 中设置：

| 变量 | 默认 | 说明 |
|---|---|---|
| `STS2MCP_REPO` | 由脚本自动计算 | 仓库根目录。桥接层从临时副本加载，无法自行反推，必须传入 |
| `STS2MCP_PORT` | `8765` | 桥接层 HTTP 端口 |
| `STS2MCP_ATTACH_FRAME` | 未设 | 设为 `1` 时接入 Godot 帧循环。下发动作的硬前置，启动脚本已默认开启 |
| `STS2MCP_CHOICE` | 未设 | 设为 `1` 时接管选牌（弃牌/检索/留牌）。**会绕过选牌 UI** —— 无人应答时等 180 秒兜底。清掉即恢复手动点选 |
| `STS2MCP_BRIDGE_DLL` | 自动推导 | 覆盖桥接层 dll 路径，调试用 |

游戏安装位置由 `launch-with-profiler.ps1` 自动探测（注册表取 Steam 根目录 →
`libraryfolders.vdf` 枚举全部库），无需配置；探测失败时可用 `-GameExe` 指定。

## 目录结构

```
src/Sts2Profiler/   CoreCLR Profiler (C++)，负责 IL 注入
src/Sts2Bridge/     游戏内托管桥接层 (C#)
src/mcp_server/     Python MCP server（单文件，薄封装）
scripts/            构建、启动与辅助脚本
docs/spec.md        设计、任务清单、以及每一处踩坑的根因
docs/game-model.md  实测得到的游戏运行时数据结构地图
backup/             试验期间改动过的游戏文件原件（现已全部还原）
```

## 开发须知

几条不知道就一定会踩的：

- **桥接层不得编译期引用任何游戏侧程序集**，`GodotSharp` 也不行 ——
  游戏用自定义 `AssemblyLoadContext`，编译期引用会导致程序集被重复加载、
  类型标识冲突，游戏在桥接层加载后约 10 毫秒硬崩溃。一律用反射。
- **`.cmd` / `.bat` 必须 CRLF**，`.ps1` 含中文时必须 **UTF-8 with BOM**。
  已由 `.gitattributes` 约束换行符，BOM 需自行保证。
- **不要用 `CardCmd.AutoPlay` 出牌** —— 它是给「劫掠」这类自动打出效果用的，
  且不消耗能量。正确路径是 `CardModel.TryManualPlay(target)`，
  即游戏自己的手动出牌入口，详见 `docs/game-model.md`。
- **签名一律从程序集元数据核对，不要照 `sts2.xml` 的注释推断** ——
  注释里没有签名，猜错过三次。工具链见 `docs/spec.md` §0.2。
- 重新编译 profiler 前须结束游戏进程（桥接层已改为加载临时副本，不受此限）。

## 边界

- **仅用于单机 / 离线游玩。** 游戏含完整多人模式，在多人局中注入即作弊，
  会影响他人并可能导致封号。本项目不支持、不用于多人模式。
- v0.107.1 为抢先体验版，游戏更新可能改变 `sts2.dll` 结构导致注入失效。
