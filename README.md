# sts2-mcp

让 Claude 通过 MCP 自动游玩《杀戮尖塔 2》(Slay the Spire 2)。

> **状态：阶段 0 —— 骨架已建，注入方案尚未验证。**
> 项目成败取决于 `DOTNET_STARTUP_HOOKS` 能否注入游戏进程（见 `docs/spec.md` 阶段 1.2）。

## 这是什么

一个游戏内 C# 桥接层 + 一个 Python MCP server，让 Claude 能够：

- 读取精确的游戏状态（手牌、能量、怪物意图与伤害数字、遗物、地图……）
- 执行游戏动作（出牌、结束回合、选牌、走地图、商店、事件）
- 自主爬塔

## 架构

```
Claude Code ──MCP(stdio)──> MCP Server (Python) ──HTTP──> Sts2Bridge (游戏进程内 C#)
  决策                      工具契约 / 自动驾驶循环        Harmony patch + 状态导出 + 动作执行
```

**设计原则：不修改游戏文件夹的任何一个字节。** 通过 `DOTNET_STARTUP_HOOKS`
环境变量注入，游戏 dll 仅作只读编译期引用。Steam 更新和"验证文件完整性"
不会破坏本项目。

## 为什么可行

《杀戮尖塔 2》是 Godot 4 + C#/.NET 9，且：

- `sts2.dll` **完全未混淆**（`CombatManager`、`Monster`、`Relic` 等均为明文）
- 随游戏附带 `sts2.xml` —— **5.2 MB 官方 API 文档注释**，命名空间 `MegaCrit.Sts2.*`
- 游戏自带 `0Harmony.dll`（运行时补丁库）
- 存在与 UI 解耦的命令层：`CardCmd.AutoPlay`、`PlayerCmd.EndTurn`
- 多人模式迫使游戏把玩家决策抽象为可注入的 `PlayerChoiceContext`，
  并提供了为序列化设计的 `NetFullCombatState`

详见 `docs/spec.md`。

## 环境要求

| 依赖 | 版本 | 状态 |
|---|---|---|
| Slay the Spire 2 | v0.107.1 | ✅ 已安装 |
| .NET SDK | 9.x | ⬜ 待安装 |
| Python | 3.12+ | ✅ 已安装 |
| ILSpy（可选，反编译用） | — | ⬜ |

## 快速开始

```bash
cp config.example.json config.json   # 然后修改其中的游戏路径
```

（后续步骤待阶段 1.2 验证通过后补充）

## 目录结构

```
src/Sts2Bridge/     游戏内 C# 桥接层（编译产物为 startup hook dll）
src/mcp_server/     Python MCP server
scripts/            启动与辅助脚本
docs/spec.md        完整设计与任务清单
```

## 边界

- **仅用于单机 / 离线游玩。** 游戏含完整多人模式，在多人局中注入即作弊，
  会影响他人并可能导致封号。本项目不支持、不用于多人模式。
- v0.107.1 为抢先体验版，游戏更新可能改变 `sts2.dll` 结构导致补丁失效。
  所有 Harmony patch 集中于 `GamePatches.cs`，便于快速修复。
