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
| 2 状态导出 | 🔨 地基完成（HTTP 服务 + 运行时反射探索），导出待实现 |
| 3 动作执行 | 📋 路径已测绘确认，待实现 |
| 4 传输层（进程内 HTTP） | ✅ |
| 5 Python MCP server | ⬜ |
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
http://127.0.0.1:8765/health                    存活状态
http://127.0.0.1:8765/describe?type=<类型全名>   列出类型成员
http://127.0.0.1:8765/eval?expr=<表达式>         即时求值只读表达式
```

`/eval` 是开发期的核心工具 —— 可在游戏运行时探查任意对象，
无须改代码重启。例：

```
/eval?expr=MegaCrit.Sts2.Core.Combat.CombatManager::Instance._state.Enemies[0]
```

不想启用时，把 Steam 启动选项清空即可，游戏恢复原样。

### 配置

本项目一律用环境变量配置，不用配置文件 —— 桥接层运行在游戏进程内、启动时序
极早，读文件需处理路径解析与失败回退；而环境变量本就要由启动脚本注入
（profiler 那三个就是），顺带多传几个零成本。

在 `scripts/launch-steam.cmd` 中设置：

| 变量 | 默认 | 说明 |
|---|---|---|
| `STS2MCP_REPO` | 由脚本自动计算 | 仓库根目录。桥接层从临时副本加载，无法自行反推，必须传入 |
| `STS2MCP_PORT` | `8765` | 桥接层 HTTP 端口 |
| `STS2MCP_ATTACH_FRAME` | 未设 | 设为 `1` 时尝试接入 Godot 帧循环（实验性，见 spec） |
| `STS2MCP_BRIDGE_DLL` | 自动推导 | 覆盖桥接层 dll 路径，调试用 |

游戏安装位置由 `launch-with-profiler.ps1` 自动探测（注册表取 Steam 根目录 →
`libraryfolders.vdf` 枚举全部库），无需配置；探测失败时可用 `-GameExe` 指定。

## 目录结构

```
src/Sts2Profiler/   CoreCLR Profiler (C++)，负责 IL 注入
src/Sts2Bridge/     游戏内托管桥接层 (C#)
src/mcp_server/     Python MCP server（待实现）
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
  且不消耗能量。正确路径见 `docs/game-model.md`。
- 重新编译 profiler 前须结束游戏进程（桥接层已改为加载临时副本，不受此限）。

## 边界

- **仅用于单机 / 离线游玩。** 游戏含完整多人模式，在多人局中注入即作弊，
  会影响他人并可能导致封号。本项目不支持、不用于多人模式。
- v0.107.1 为抢先体验版，游戏更新可能改变 `sts2.dll` 结构导致注入失效。
