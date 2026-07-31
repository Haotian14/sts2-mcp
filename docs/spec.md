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
| ~~出牌~~ ⚠️ 见下方更正 | ~~`Core.Commands.CardCmd.AutoPlay(...)`~~ |
| **出牌（实测确认）** | 构造 `GameActions.PlayCardAction` → `RunManager.Instance.ActionQueueSet.EnqueueWithoutSynchronizing(action)` → `await action.CompletionTask`（详见 `game-model.md`） |
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

- [x] 1.1 最小 startup hook dll（零外部依赖，仅写日志）—— `src/Sts2Bridge/StartupHook.cs`
- [x] 1.2 **用 `DOTNET_STARTUP_HOOKS` 验证注入 → ❌ 失败**
- [x] 1.2b **经 `runtimeconfig.json` 的 `configProperties` 注入 → ❌ 同样失败**
- [x] 1.3a **CoreCLR Profiler 注入验证 → ✅ 成功（见 §1.3a 结论）**
- [x] 1.3b-1 元数据侦察：确定 IL 注入落点 → ✅ `NGame..cctor`
- [x] 1.3b-2 **实施 IL 注入，加载托管桥接层 → ✅ 成功**

#### 1.3b-2 结论：注入链路全线打通

调用栈证据（`logs/bridge.log`）：

```
[0] Sts2Bridge.Entry::Initialize
[1] System.RuntimeType::CreateInstanceDefaultCtor
[2] MegaCrit.Sts2.Core.Nodes.NGame::.cctor      <- 注入点
[3] MegaCrit.Sts2.Core.Nodes.NGame::_EnterTree
[4] Godot.Node::InvokeGodotClassMethod
```

`NGame..cctor` 由 27 字节改写为 53 字节（Tiny header 0xD6），注入 26 字节：

```
ldstr    "<Sts2Bridge.dll 绝对路径>"
call     Assembly::LoadFrom(string)
ldstr    "Sts2Bridge.Entry"
callvirt Assembly::GetType(string)
call     Activator::CreateInstance(Type)
pop
<原 27 字节 IL>
```

阶段 2/3 所需的关键类型均已确认可反射到：`NGame`、`CombatManager`、
`RunManager`、`CardCmd`、`PlayerCmd`、`CardModel`、`NetFullCombatState`、
`JsonSerializationUtility`。

##### 踩坑：注入的 IL 不可直接 call 自有程序集

初版注入的是 `ldstr path; call LoadFrom; pop; call Entry::Initialize()`，
运行时顺序看似正确，实际抛：

```
System.IO.FileNotFoundException: Could not load file or assembly 'Sts2Bridge, Version=1.0.0.0'
```

**方法体是整体 JIT 的**：JIT 编译 `.cctor` 时即需解析其中每一个 `call`
的目标，该过程发生在同一方法体内 `LoadFrom` 执行**之前**，于是 CLR 按
默认探测路径去游戏目录寻找 `Sts2Bridge.dll` 而失败。运行时顺序正确，
JIT 时序不正确。

**规则：注入的 IL 只能引用 BCL 类型，对自有程序集一律走反射。**
改用 `Assembly.GetType` + `Activator.CreateInstance` 后通过，代价是入口
类型须可实例化（`Entry` 由 static class 改为 sealed class，构造函数调用
`Initialize()`）。

##### 踩坑：重新编译前须先结束游戏进程

游戏进程持有 `Sts2Profiler.dll`，未退出时链接会失败：
`LINK : fatal error LNK1104: 无法打开文件 Sts2Profiler.dll`。

---

### ⚠️ 硬性约束：桥接层不得编译期引用任何游戏侧程序集

**包括 `GodotSharp`。** 2026-07-30 实测，一旦在 `Sts2Bridge.csproj` 中引用
`GodotSharp`，游戏会在桥接层加载后约 10 毫秒硬崩溃 —— 崩得太早，托管侧的
异常兜底都来不及写日志。

**根因**：游戏使用自定义 `AssemblyLoadContext` 加载自身程序集，而桥接层由
注入的 `Assembly.LoadFrom` 载入 **Default ALC**。编译期引用会使 CLR 在
Default ALC 中**再加载一份** `GodotSharp`，进程内遂存在两个实例、两套类型
标识，而 Godot 的 native 绑定状态全局唯一 —— 必崩。

诊断证据（`logs/profiler.log` 中同一 dll 出现两个不同 AssemblyID）：

```
23:15:28.318  GodotSharp.dll  AssemblyID=0x...70CB1DA0   <- 游戏自己的
23:15:28.696  GodotSharp.dll  AssemblyID=0x...6F146FB0   <- 引用所触发
```

`<Private>false</Private>` 并不能规避此问题 —— 它只控制是否复制副本到输出
目录，不影响运行时由哪个 ALC 解析该引用。

**规则：一律通过反射访问 Godot 与 sts2 的类型。** 反射经
`AppDomain.CurrentDomain.GetAssemblies()` 取到的是游戏 ALC 中已存在的实例，
不会引入第二份。校验方法：编译后扫描 `Sts2Bridge.dll`，其中不应出现
`Godot` 字样。

**教训**：该次改动同时引入了「GodotSharp 编译期引用」与「帧循环接入」两项
变更，导致崩溃后无法直接判定祸首，帧循环接入因此背了一整个阶段的黑锅 ——
后来单独验证，它一次就通过了（见 §1.4）。**一次只引入一个变量。**

帧循环接入由 `STS2MCP_ATTACH_FRAME` 控制，现已在两个启动器中**默认开启**；
怀疑它导致问题时可传 `-NoAttachFrame` 或注释掉 `.cmd` 中那一行来排除。

## 阶段 1 完成

注入链路：**Profiler (C++, 环境变量启用) → IL 注入 `NGame..cctor` →
托管桥接层 → 游戏 API**，全程**游戏目录零改动**。

#### 1.3b-1 结论：注入落点为 `MegaCrit.Sts2.Core.Nodes.NGame..cctor`

侦察实测（`ProbeForInjectionSite`）：

```
NGame.get_Instance         Tiny  code=6   1A 7E F2 05 00 04 2A   ldsfld 0x040005F2; ret
NGame..cctor               Tiny  code=27  6E 22 00 00 F0 44 ...
CombatManager..cctor       Tiny  code=11  2E 73 F2 85 00 06 80 6E 2E 00 04 2A
RunManager..cctor          Tiny  code=11  2E 73 F8 0B 00 06 80 F7 03 00 04 2A
```

`CombatManager..cctor` 可解为 `newobj CombatManager(); stsfld _instance; ret`，
语义与字节数吻合，确认解析无误。

**选择 `NGame..cctor` 的理由**（三个条件同时满足）：

| 条件 | 说明 |
|---|---|
| 只执行一次 | CLR 保证静态构造函数仅运行一次 → **无需防重入 guard** |
| Tiny 格式 | Tiny 不允许异常段 → **无需修正异常表绝对偏移**（IL 注入最易崩的地方） |
| 足够早 | 在 `NGame` 任何静态成员被访问前执行 |

被否决的备选：`get_Instance` 虽同为 Tiny，但每帧被调用多次，注入后每次都会
执行 `Assembly.LoadFrom`（含文件系统访问），必须额外加 guard；
`<Module>` 的模块初始化器不存在（`EnumMethods` 返回 S_FALSE）。

预计注入后长度：16（注入）+ 27（原）= 43 字节，仍在 **Tiny 上限 63 字节**
之内，无需转换为 Fat header。

##### 踩坑：`CorILMethod_FormatMask` 不能用于判断 Tiny/Fat

`corhdr.h` 中 `CorILMethod_FormatMask == 0x7`，但 CLR 自身的
`IMAGE_COR_ILMETHOD_TINY::IsTiny()` 使用的是 `(CorILMethod_FormatMask >> 1)`
即 **`0x3`**，且 `GetCodeSize()` 用 `>> (CorILMethod_FormatShift - 1)` 即 `>> 2`。

误用 `0x7` 会将 `b0 = 0x1E / 0x2E / 0x6E` 这类（低 2 位为 2，确属 Tiny）
误判为未知格式 —— 恰好覆盖了大量 `.cctor` 与 setter，一度让人以为
`.cctor` 方法体不可读、理想落点不存在。**判断格式只看低 2 位。**
- [ ] 1.4 Harmony hello-world：patch `CombatManager.StartTurn` 并输出日志
- [ ] 1.5 取得关键单例引用（`RunState` / `CombatState` / `Player`）

#### 1.3a 结论：Profiler 注入成立，游戏目录零改动

2026-07-30 实测通过。`src/Sts2Profiler/`，产物 `bin/Sts2Profiler.dll`（x64）。

```
[22:17:02.180] DllMain: DLL_PROCESS_ATTACH  (pid=43756)
[22:17:02.181] ClassFactory::CreateInstance —— CLR 正在请求 profiler 实例
[22:17:02.181]   PROFILER LOADED  —— Initialize 被调用
[22:17:02.182] 可用接口      : ICorProfilerInfo8  [✓]
[22:17:02.182] SetEventMask  : hr=0x00000000 (成功)
[22:17:02.346] *** 命中目标模块: ...\sts2.dll
[22:17:02.346]     ModuleID=0x00007FFDC4175380  AssemblyID=0x0000022A40FDB960
```

| 结论 | 说明 |
|---|---|
| profiler 可被加载 | `Initialize` 在进程启动 1 毫秒内被调用 |
| 可拦截 `sts2.dll` 加载 | 距启动 0.35 秒，取得 `ModuleID` / `AssemblyID` |
| **`ICorProfilerInfo8` 可用** | 最高接口版本 —— 完整 IL 重写与 ReJIT 能力可用 |
| 游戏目录改动 | **零**，仅三个环境变量 |

启用方式（Steam 启动选项）：

```
cmd /C "set CORECLR_ENABLE_PROFILING=1 && set CORECLR_PROFILER={27585C9F-BB81-4251-B62F-1B463AB4D58A} && set CORECLR_PROFILER_PATH_64=<仓库>\bin\Sts2Profiler.dll && %command%"
```

验证亦可用 `scripts/launch-with-profiler.ps1 -Direct`（直接启动 exe）：
游戏虽会因缺少 Steam appID 而退出，但 profiler 的 `Initialize` 远早于
Steamworks 初始化，不影响验证。

#### 构建环境踩坑记录

| 问题 | 现象 | 解法 |
|---|---|---|
| Windows SDK 10.0.26100 不含 profiling 头文件 | 无 `cor.h` / `corprof.h` / `corhdr.h` | 取自 `dotnet/runtime` release/9.0，置于 `src/Sts2Profiler/include/`。注意 `cor.h` 与 `corhdr.h` 在 `src/coreclr/inc/`，而 `corprof.h` 与 `corerror.h` 在 `src/coreclr/pal/prebuilt/inc/` |
| MSVC 按系统代码页(936)读源文件 | C4819 + C2001「常量中有换行符」 | 编译加 `/utf-8` |
| PowerShell 5.1 按 ANSI 读 `.ps1` | 中文全部乱码、语法报错 | 含非 ASCII 的 `.ps1` **必须存为 UTF-8 with BOM** |
| `.bat` 用 LF 换行 | cmd 解析多行 `for` 结构崩溃 | 改用 `.ps1`（已弃用 build.bat） |
| `/Fo:"$OutDir\"` | 结尾反斜杠紧邻引号被解析为转义引号 `\"`，吞掉后续全部参数 | 指定完整 obj 文件名，不用目录形式 |
| profiler 日志显示乱码 | `/utf-8` 使写出的是 UTF-8 字节 | 读取时 `Get-Content -Encoding UTF8` |

#### 1.2 结论：`DOTNET_STARTUP_HOOKS` 对本游戏无效

2026-07-30 实测。测试有效性已确认——游戏确实启动并执行了 C# 代码
（日志含 `MegaCrit.Sts2.Core.Nodes.NGame._EnterTree()` 调用栈），
但 hook 未被调用，日志文件未生成。

**原因**：游戏日志首行为 `MegaDot v4.5.1.m.12.mono.custom_build`。
Godot 的 .NET 集成是**原生 exe 先启动，再经 `hostfxr` 嵌入式加载 CoreCLR**
（`hostfxr_initialize_for_runtime_config` 路径）。而 `DOTNET_STARTUP_HOOKS`
由 `hostpolicy` 在**标准应用启动路径**（`hostfxr_run_app`）中读取并转为
`STARTUP_HOOKS` AppContext 属性。嵌入式加载不经过该路径，故环境变量无人读取。

**1.2b 亦失败**：改用 `runtimeconfig.json` 的 `configProperties` 直接设置
`STARTUP_HOOKS` 属性（经 Steam 启动，`Steamworks initialization succeeded!`，
C# 正常运行），hook 依然未执行。补丁已还原，游戏目录 SHA256 与备份一致。

**根因（两次失败指向同一处）**：CoreCLR 的 startup hook 由
`StartupHookProvider.ProcessStartupHooks()` 在 **`coreclr_execute_assembly`**
的执行路径中触发。Godot 使用 `hostfxr` 的
`load_assembly_and_get_function_pointer` 加载托管代码，**从不调用该函数**，
故整段代码路径不可达 —— 属性来自环境变量还是 `configProperties` 均无差别。

这是 Godot .NET 游戏的固有特性，非配置错误。**「零改动 + 纯 C#」目标不成立。**

#### 后续方案（三选一）

| 方案 | 改游戏目录 | 前置 | 说明 |
|---|---|---|---|
| **P. CoreCLR Profiler** | 否 | VS Build Tools + Windows SDK（数 GB） | `CORECLR_ENABLE_PROFILING` / `CORECLR_PROFILER` / `CORECLR_PROFILER_PATH` 三个环境变量。Profiler 由 EEStartup 在 CLR 初始化最早期加载，**与 host 启动路径无关** —— 恰好绕开 startup hook 的死穴。需写 native COM dll（C++） |
| **M. 托管 DLL 代理** | 是（替换 dll） | 无（.NET SDK 已装） | 将游戏依赖的某个托管 dll 换成同名代理程序集，以 `[assembly: TypeForwardedTo]` 转发全部类型至改名后的原 dll，并用 `[ModuleInitializer]` 触发自身初始化。纯 C#，当日可验证。风险：转发遗漏导致游戏崩溃；Steam 验证文件会还原 |
| **W. 不注入，走外部视觉** | 否 | 无 | 截图 + 模拟点击。零风险、当日可玩，但脆弱、慢、耗 token |

**本机工具链现状**：无 VS Build Tools、无 Windows SDK、无 clang/gcc/rust
（方案 P 需先行安装）；.NET 9 SDK 9.0.316 已装；Python 3.12 已装。

**`deps.json` 附带发现**：`0Harmony` 2.4.2.0 是 `sts2` 的**直接依赖** ——
游戏自身就在使用 Harmony，而非仅打包。其余直接依赖：`GodotSharp` 4.5.1、
`SmartFormat` 3.3.0、`Steamworks.NET`、`Sentry` 5.0.0、`MonoMod.Backports`、
`JetBrains.Annotations`、`System.IO.Hashing`、`Vortice.DXGI`。
pck 中不存在 `gdextension` / `addons/` / `plugin.cfg` 等扩展点。

#### 附带发现

- **StS2 的 Steam appid = `2868840`**（据 `controller_config\game_actions_2868840.vdf`
  及安装体积 2.71 GB 吻合）。可用 `steam://rungameid/2868840` 启动。
- 直接运行 `SlayTheSpire2.exe` 会因 `Steamworks initialization failed!
  No appID found` 而在数次重试后退出。测试必须经 Steam 启动，
  或往游戏目录放 `steam_appid.txt`（后者需改动游戏文件夹）。
- Godot 日志位于 `%APPDATA%\SlayTheSpire2\logs\godot.log`，含完整 C# 异常栈，
  是后续调试的主要窗口。

> **在注入通过之前，不应编写任何其他代码。**

### 阶段 2 · 状态导出（C#）

- [x] 2.1 主线程调度器（`ConcurrentQueue<Action>` + 逐帧排空）——
      **已接入 Godot 帧循环**，不再降级运行。见下方「1.4 帧循环接入」。
- [x] 2.2 结论：**`NetFullCombatState` 不可用**
- [x] 2.3 `StateExporter` → `GET /state`
- [ ] 2.4 非战斗状态：卡牌奖励、地图可走节点、商店库存、事件选项、休息点、Boss 遗物三选一
- [x] 2.5 **状态压缩** —— 实测 `/state` 1.5 KB、`/glossary` 1.6 KB，在预算内

#### 2.2 结论：`NetFullCombatState` 不可用

它确实存在（`MegaCrit.Sts2.Core.Entities.Multiplayer.NetFullCombatState`）且
结构紧凑，但只有三个成员，装的是多人同步快照：

```
Creatures : List<CreatureState>   monsterId, playerId, currentHp, maxHp, block, powers
Players   : List<PlayerState>     characterId, turnNumber, phase, energy, stars,
                                  gold, piles, potions, relics, orbs, rng...
Rng       : SerializableRunRngSet
```

**缺的恰好是决策最需要的三样**：怪物意图与伤害数字、卡牌可打性、卡面文本。
且标识全是 `ModelId` 而非可读名称。故仍需手写导出，其字段选取可作参照。

#### 2.3 结论：两个接口，动静分离

| 接口 | 内容 | 频次 |
|---|---|---|
| `GET /state` | 只发**会变的数字**：HP/能量/意图/手牌索引与可打性/牌堆计数 | 每个决策点 |
| `GET /glossary` | 只发**不变的文本**：卡牌与遗物的标题和描述 | 一局一次，由 MCP server 缓存 |

拆分的理由是 token 成本：卡面文本每回合一字不变，塞进 `/state` 等于每回合
重传一遍，成本随回合数线性增长。实测两者各约 1.5 KB，若合并则 `/state`
每次都要背着那 1.6 KB。

标识一律用**模型类型短名**（`StrikeSilent` / `CorpseSlug`）而非本地化中文：
稳定、与语言设置无关、可作字典键，也让阶段 6.5 的决策日志能跨局比对。

##### 踩坑：`IsPlayable` 不是「现在能不能打」

实测诅咒牌 `AscendersBane` 的 `IsPlayable` 为 `true`、`EnergyCost.Canonical`
为 `-1`（而 `CostsX` 为 `false`）。照 `IsPlayable` 做决策，模型会反复尝试
打出诅咒牌。

正确来源是 `CardModel.CanPlay(out UnplayableReason, out AbstractModel)` ——
游戏用来把卡牌置灰的那套判定，能量不足、诅咒、被敌人封锁全部涵盖，并顺带
给出原因，恰好也是 3.6 所需。负费用一律对外输出 `null`，避免模型拿 `-1`
去做能量运算。

##### 踩坑：意图伤害必须调用委托才有值

`AttackIntent.DamageCalc` 是 `Func<decimal>`，延迟计算；
`IntentLabelFormat.Variables` 要到渲染时才填，实测恒为空字典，
拿不到现成数字。必须 `DynamicInvoke()`。

交叉验证：导出的意图为怪 0「3×2」、怪 1「8」，结束回合后玩家 HP 恰好
`56 → 42`（−14），与预测完全吻合。

#### 1.4 帧循环接入成立 —— 阶段 3 的前置已解除

```
[Entry] 于主线程尝试接入帧循环
[MainThread] 已接入帧循环 (Godot.SceneTree)
/health → {"attached":true,"frame":1453}
```

**关键在于「在哪个线程订阅」**。此前的实现从后台线程反射调用
`Engine.GetMainLoop()` 并订阅 `SceneTree.ProcessFrame` —— 订阅本身就是一次
Godot 原生调用，在后台线程做才是真正的隐患。

改为在 `Entry.Initialize()` 内订阅即可：该处由 `NGame..cctor` 触发，调用栈为
`NGame._EnterTree -> .cctor`，是全流程中**唯一已确证位于主线程**的时机；
且此刻 NGame 正在进入场景树，`SceneTree` 必然已存在。实测**第一次尝试即成功**。

后台线程重试的老路径保留为兜底，仅在主线程时机失败时才走。

**降级路径仍然保留，但下发动作时绝不可依赖它** —— 从后台线程往
`ActionQueueSet` 入队会损坏队列结构。阶段 3 的动作接口须先检查 `IsAttached`。

##### 踩坑：以为不通的东西，其实从没被执行过

排查时发现 `bridge.log` 历史记录里清一色是
「跳过帧循环接入（未设 `STS2MCP_ATTACH_FRAME=1`）」——
**`TryAttach()` 从来没有真正运行过一次**。

先前把它判为高风险，依据是「上次引入帧循环接入后游戏 10 毫秒硬崩溃」，
但那次同时引入了 GodotSharp 编译期引用，而崩溃根因已由 profiler.log 中两个
不同的 `AssemblyID` 确证为后者。两个变量绑在一起改，导致无辜的那个背了锅，
并因此被搁置了整整一个阶段。

##### 踩坑：环境变量属于启动器进程，改文件不影响已在运行的启动器

上述结论差点又被埋一次。改好脚本后连试两次仍显示「未设」，进程树才揭示：

```
cmd.exe (launch-steam.cmd)  启动于 20:51   ← 脚本修改时间为 21:02
  └ SlayTheSpire2.exe       启动于 21:07
```

承载环境变量的 `cmd.exe` 早于修改启动，其后「重启游戏」只是在同一个启动器
进程下换了个子进程，脚本根本没有被重新读取。判据是 `logs/launcher.log` ——
若本次启动没有新的 `launcher invoked` 记录，说明启动器进程是旧的。

##### 踩坑：两个启动器的环境变量必须同步

`launch-steam.cmd` 与 `launch-with-profiler.ps1` 各自设置环境变量。
只改其一会造成「代码为何不生效」式的假象。新增变量时两处都要加。

### 阶段 3 · 动作执行（C#）

- [ ] 3.1 `play_card(hand_index, target_index)` → 构造 `PlayCardAction` 并经
      `RunManager.Instance.ActionQueueSet.EnqueueWithoutSynchronizing` 入队，
      等待 `CompletionTask`。
      **更正**：早期版本此处写的是包装 `CardCmd.AutoPlay`，那是错的 ——
      官方文档明确其为「for free, non-player-choice card playing effects」
      （服务于劫掠、复制药水一类自动打出效果，**且不消耗能量**）。
      `PlayerChoiceContext` 可直接 `new`（基类仅一个用于向远程玩家显示归因的
      模型栈）。详见 `game-model.md`。
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
