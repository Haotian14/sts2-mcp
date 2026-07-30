# sts2-mcp 设计与任务清单

日期：2026-07-30
目标游戏版本：Slay the Spire 2 **v0.107.1**（构建于 2026-06-18，commit `59260271`）

---

## 1. 已验证的事实

以下均为在本机游戏安装目录中直接查证所得，非推测。

| 事实 | 证据 |
|---|---|
| 引擎为 Godot 4 + C#/.NET 9（自包含 9.0.7） | `sts2.runtimeconfig.json` → `"tfm": "net9.0"`；目录含 `GodotSharp.dll`、`coreclr.dll`、`libspine_godot...dll` |
| `sts2.dll`（9.36 MB）**完全未混淆** | ASCII 扫描命中明文类名：`Monster`×483、`Relic`×795、`Potion`×571、`CombatManager`、`RunState`、`Intent`、`DrawPile`、`EndTurn` |
| 随包附带 **5.2 MB 官方 API 文档** | `sts2.xml`，含每个类/方法的 `<summary>`，命名空间根为 `MegaCrit.Sts2.*` |
| 游戏自带 Harmony 与 MonoMod | `0Harmony.dll`、`MonoMod.Backports.dll`、`MonoMod.ILHelpers.dll` |
| 启动钩子**未被禁用** | `runtimeconfig.json` 仅关闭 `MetadataUpdater` 与 `BinaryFormatter`，未设 `System.StartupHookProvider.IsSupported=false` |
| 无官方 mod 加载器 | 全盘搜索 `mod` 仅命中 `fmod` 与 `MonoMod` |

### 1.1 游戏白送的关键 API

| 用途 | API |
|---|---|
| 出牌（与 UI 解耦） | `Core.Commands.CardCmd.AutoPlay(PlayerChoiceContext, CardModel, Creature, AutoPlayType, bool, bool)` |
| 结束回合 | `Core.Commands.PlayerCmd.EndTurn(Player, bool, Func<Task>)` |
| 合法性判定 | `Core.Models.CardModel.CanPlay(out UnplayableReason, out AbstractModel)` |
| 动作时序同步 | `Core.Combat.CombatManager.IsExecutingCardOrPotionEffect(Player)`（配 `Begin/EndCardOrPotionEffect`） |
| 回合阶段状态机 | `Core.Combat.PlayerTurnPhase`、`CombatManager.IsPlayerReadyToEndTurn(Player)` |
| 地图导航 | `Core.Runs.RunManager.EnterRoom` / `EnterMapPointInternal` / `EnterRoomDebug` |
| 战斗状态快照（为序列化而设计） | `Core.Entities.Multiplayer.NetFullCombatState`（含 `.PlayerState`） |
| 玩家决策结果的网络表示 | `Core.Entities.Multiplayer.NetPlayerChoiceResult` |
| JSON 序列化 | `Core.Saves.JsonSerializationUtility.ToJson<T>()` / `FromJson<T>()` / `AddTypeInfoResolver()` |
| 商店条目 | `Core.Entities.Merchant.MerchantCardEntry` / `MerchantRelicEntry` / `MerchantPotionEntry` / `MerchantCardRemovalEntry` |
| 测试基建（佐证逻辑可脱离 UI 驱动） | `RunManager.SetUpTest`、`SetUpReplay`、`CombatReplay`、`CombatManager.DebugForceTopCardOnNextShuffle`、`MockCraftedActMap`、`NullRunState`、`MockPotionPool` |

**关键洞察**：多人模式迫使 MegaCrit 将「玩家做决策」抽象为可注入的
`PlayerChoiceContext`，并提供了完整可序列化的战斗状态。这些本应由我们自己
硬造的抽象层，游戏已经具备。

---

## 2. 架构

```
Claude Code ──MCP(stdio)──> MCP Server (Python) ──HTTP──> Sts2Bridge (游戏进程内 C#)
  决策                      工具契约 / 自动驾驶循环        Harmony patch + 状态导出 + 动作执行
                                127.0.0.1:8765
```

### 2.1 零改动注入

Steam 启动选项：

```
cmd /C "set DOTNET_STARTUP_HOOKS=C:\Users\Administrator\Desktop\sts2-mcp\src\Sts2Bridge\bin\Release\net9.0\Sts2Bridge.dll && %command%"
```

游戏 dll 仅作**只读编译期引用**（`.csproj` 中 `<Private>false</Private>`，不复制副本）。

**关键陷阱**：不可将 `0Harmony.dll` 复制进本仓库 —— 会加载出第二个 Harmony
实例，补丁作用于不同的 CLR 类型，表现为「补丁成功但完全无效」。必须在
`StartupHook.Initialize()` 首行挂载解析钩子，重定向到游戏目录：

```csharp
AssemblyLoadContext.Default.Resolving += (ctx, name) => {
    var p = Path.Combine(GameDataDir, name.Name + ".dll");
    return File.Exists(p) ? ctx.LoadFromAssemblyPath(p) : null;
};
```

### 2.2 线程模型

Godot API 仅可在主线程调用，而 `HttpListener` 回调在后台线程。
必须 Harmony patch 一个逐帧方法，维护 `ConcurrentQueue<Action>` 并每帧排空；
HTTP 线程经由该队列提交所有游戏调用，用 `TaskCompletionSource` 取回结果。

---

## 3. 任务清单

### 阶段 0 · 环境

- [x] 0.3 建立工作目录与 git 仓库
- [ ] 0.1 安装 .NET 9 SDK（`winget install Microsoft.DotNet.SDK.9`）
- [ ] 0.2 安装 ILSpy（可选；`sts2.xml` 只有注释，看实现需反编译）
- [ ] 0.4 备份存档 `%APPDATA%\SlayTheSpire2\`

### 阶段 1 · 注入验证 ⚠️ 唯一的真风险点

- [ ] 1.1 最小 startup hook dll（仅写一行日志）
- [ ] 1.2 **用 `DOTNET_STARTUP_HOOKS` 启动游戏验证注入** ← 成败在此
- [ ] 1.3 备选方案（仅 1.2 失败时）：① 排查加载失败原因 ② BepInEx 6 CoreCLR ③ 改 `sts2.deps.json` ④ patch `sts2.dll`（侵入度递增，②及之后均需改动游戏文件夹）
- [ ] 1.4 Harmony hello-world：patch `CombatManager.StartTurn` 并输出日志
- [ ] 1.5 取得关键单例引用（`RunState` / `CombatState` / `Player`）

> **在 1.2 通过之前，不应编写任何其他代码。** 该步骤要么 20 分钟内通过，
> 要么必须更换整体方案。

### 阶段 2 · 状态导出（C#）

- [ ] 2.1 主线程调度器（`ConcurrentQueue<Action>` + 逐帧排空）
- [ ] 2.2 验证 `NetFullCombatState` 能否直接序列化（若可行则大幅简化 2.3）
- [ ] 2.3 `StateExporter`：HP/护甲/能量、手牌（名称+费用+描述+`CanPlay` 结果）、牌堆计数、每只怪的 HP/护甲/**意图与伤害数字**/增益、遗物、药水、金币、房间类型
- [ ] 2.4 非战斗状态：卡牌奖励、地图可走节点、商店库存、事件选项、休息点、Boss 遗物三选一
- [ ] 2.5 **状态压缩**：原始状态数十 KB → 压至 1–2 KB。直接决定 token 成本，易被低估

### 阶段 3 · 动作执行（C#）

- [ ] 3.1 `play_card(hand_index, target_index)` → 包装 `CardCmd.AutoPlay`（需先厘清 `PlayerChoiceContext` 构造方式）
- [ ] 3.2 `end_turn()` → 包装 `PlayerCmd.EndTurn`
- [ ] 3.3 `use_potion(i, target)`
- [ ] 3.4 非战斗动作：选牌/跳过、地图移动、商店买入与除卡、事件选项、休息点、「X 选 1」类效果
- [ ] 3.5 **就绪判据**：动作后须等到 `!IsExecutingCardOrPotionEffect` 且 `PlayerTurnPhase == Play` 方可返回，否则会读到中间状态
- [ ] 3.6 错误回传：以 `CanPlay` 预检，非法动作返回结构化错误而非崩溃

### 阶段 4 · 传输层（C#）

- [ ] 4.1 游戏内 `HttpListener` on `127.0.0.1:8765`：`GET /state`、`POST /action`、`GET /health`（**仅绑 localhost**）
- [ ] 4.2 请求 → 主线程队列 → `TaskCompletionSource` 取回结果
- [ ] 4.3 异常兜底：任何异常均不得导致游戏进程崩溃

### 阶段 5 · MCP Server（Python）

- [ ] 5.1 依赖：`mcp`、`httpx`
- [ ] 5.2 stdio 传输的 MCP server 骨架
- [ ] 5.3 工具：`get_state` / `get_legal_actions` / `play_card` / `end_turn` / `use_potion` / `choose`
- [ ] 5.4 **`auto_play_until(stop_on=[...])`** —— 全自动的核心。内部循环：读状态 → 唯一合法动作则本地执行 → 命中停止条件（卡牌奖励/地图/Boss/精英/低血量/死亡）才返回
- [ ] 5.5 工具 description 的 prompt 工程（MCP 工具描述即模型的说明书，易被低估）
- [ ] 5.6 注册到 Claude Code（`.mcp.json`）
- [ ] 5.7 用 MCP Inspector 调试（`npx @modelcontextprotocol/inspector`）

### 阶段 6 · 让它打得好

- [ ] 6.1 三档决策分流（见下）
- [ ] 6.2 战斗启发式：能斩杀则斩杀；否则按「本回合总伤害 vs 所需格挡」排序
- [ ] 6.3 决策 prompt：战斗上下文模板 + 尖塔策略要点（曲线、遗物协同、精英取舍）
- [ ] 6.4 整局 runner（Claude Code 交互式跑不完一整局）
- [ ] 6.5 决策日志 `(state, action, 理由)` —— 唯一能让它变强的东西
- [ ] 6.6 死亡/胜利检测与自动开新局

---

## 4. 全自动的决策分层

一局 StS2 有数百个决策点。若每步都在对话中调用一次工具，会耗尽上下文、
昂贵且缓慢。自动驾驶循环应位于 MCP server 内，按价值分流：

| 档 | 情况 | 决策者 |
|---|---|---|
| 1 | 仅有一个合法动作 | MCP server 本地执行，**不询问模型** |
| 2 | 战斗内常规出牌 | 本地启发式，或廉价模型 |
| 3 | 高价值决策：卡牌奖励、地图路线、Boss 遗物、商店、精英取舍 | **询问 Claude** |

档 1 可吸收绝大部分动作量，使单局模型调用从数百次降至数十次。

---

## 5. 关键路径

```
1.2 注入验证 → 1.5 取得游戏对象 → 2.3 状态导出 → 3.1+3.2 出牌/结束回合
    → 4.1 HTTP → 5.2+5.3 MCP
```

至此即可在 Claude Code 中端到端运行。阶段 2.4 / 3.4（非战斗）与阶段 6
（自动化与策略）均为增量。

---

## 6. 边界与风险

1. **仅限单机 / 离线。** `release_info.json` 含 `main_assembly_hash`，游戏具备
   完整多人模式。在多人局中注入即作弊，影响他人并可能导致封号。
   本项目不支持、不用于多人模式。
2. **v0.107.1 为抢先体验版**，游戏更新可能改变 `sts2.dll` 结构使补丁失效。
   所有反射与 Harmony patch 集中于 `GamePatches.cs`，以便快速修复。
3. **1.2 未通过则「零改动」目标不成立**，须退化至侵入式方案（见 1.3）。
