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
- [x] 0.2 **离线反编译工具链**（不是可选项，是必需品 —— 见下）
- [ ] 0.4 备份存档 `%APPDATA%\SlayTheSpire2\`（**技术债**：调试期重启频繁，
      每次都回退到最近存档点；跑长局前务必补上）
- [x] 0.5 **一键重启** `scripts/restart-game.ps1` —— 桥接层由 profiler 在进程
      启动那一刻载入，每改一次都得重启游戏才能验证。脚本走完：结束进程 →
      等端口释放 → 经 Steam 启动 → 等接入帧循环 → 点主菜单「继续游戏」。
      三个踩坑固化其中：
      - **主菜单加载晚于桥接层就绪**，HTTP 通了主菜单还没构建完，
        第一次跑误判成「没有存档」。须等 `can_resume` 出现
      - **重启只做一半不够**：游戏停在主菜单，不点「继续游戏」就一直没有
        `RunState`。为此加了 `POST /action/resume_run` 与 `/state` 的
        `in_run` / `can_resume` —— 否则上层只能从「所有字段都是 null」去猜
      - **`.ps1` 含中文必须存成 UTF-8 with BOM**（仓库早有记录，本次又踩）

#### 0.2 结论：签名必须从元数据核对，不能从文档注释推断

`sts2.xml` 只有 `<summary>`，不含签名细节。阶段 3 动手前照注释推断出的出牌
路径，实测三处全错（构造函数参数、入队方法、`PlayerChoiceContext` 可否
new），详见 `game-model.md` 的「更正记录」。**猜的成本远高于装工具的成本。**

```powershell
dotnet tool install -g ilspycmd --version 9.1.0.7988
$env:DOTNET_ROLL_FORWARD = 'Major'     # 该版本 target net8.0，本机只有 9
ilspycmd "<游戏目录>\data_sts2_windows_x86_64\sts2.dll" -o <输出目录>
```

37 秒产出单个 17.9 MB 的 `sts2.decompiled.cs`，全文可 grep —— 单文件反而
比按类型拆目录好用，「谁调用了 X」一次搜索即得。

不装 ilspycmd 时，用 `MetadataLoadContext` 写十几行也能列出签名（只读元数据、
不执行任何游戏代码），够用于「这个方法收什么参数」这类问题。

**游戏目录是自包含运行时**（200 个 dll 全在
`data_sts2_windows_x86_64\`），反编译时无须额外配置引用路径。

### 阶段 1 · 注入验证 ⚠️ 唯一的真风险点

- [x] 1.1 最小 startup hook dll（零外部依赖，仅写日志）——
      源文件已删除（该路线经 1.2 证伪，代码无人引用；结论保留在本节）
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
后来单独验证，它一次就通过了（见 §2.1 结论）。**一次只引入一个变量。**

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
- [~] 1.4 ~~Harmony hello-world~~ —— **作废**。当初设想用 Harmony 打补丁，
      实际走的是 profiler IL 注入 + 全程反射，从未需要 Harmony
- [x] 1.5 取得关键单例引用 —— 由 `GamePaths` 达成（见阶段 2/3）

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
      **已接入 Godot 帧循环**，不再降级运行。见下方「2.1 结论」。
- [x] 2.2 结论：**`NetFullCombatState` 不可用**
- [x] 2.3 `StateExporter` → `GET /state`
- [x] 2.6 **意图伤害改用 `GetSingleDamage` / `GetTotalDamage`**（原 `DamageCalc`
      不含力量等修正，见下方踩坑记录）—— 已实机复验
- [x] 2.7 **血量移出 `if (inCombat)`**：实测战斗外
      `RunState.Players[0].Creature.CurrentHp` 照样可读（结算界面上仍是 31/70），
      而地图选路、要不要打精英、休息点烤火还是打铁，每个非战斗决策都要用血量。
      原先 `/state` 在战斗外只给章节层数与金币，缺了最关键的一项 —— 已实机复验
- [x] 2.8 **`awaiting_choice` 进 `/state`**：「游戏正等你做选择」是一等一的
      状态事实，且**无法从状态数字反推**（选择期间手牌张数不变）。
      不明说上层就只能猜 —— 待实机验证
- [x] 2.4a 地图可走节点 → `/state` 的 `map` 段（见 3.4b）
- [x] 2.4b 非战斗状态 → 统一为 `/state` 的 `screen` 段：奖励、卡牌三选一、
      休息点、宝箱、事件、商店（带价格）、主菜单、游戏结束，均已导出。
      商店与 Boss 遗物待实机验证
- [x] 2.5 **状态压缩** —— 实测 `/state` 1.5 KB、`/glossary` 1.6 KB，在预算内

#### 2.1 结论：帧循环接入成立，阶段 3 的前置已解除

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

##### 踩坑（续）：上面那次交叉验证，恰好验不出真正的 bug

`DamageCalc` 给的是**基础伤害，不含力量等修正**。上面那次之所以对得上，
纯粹因为当时两只怪都没有任何伤害修正 —— **一次成功的验证并不能证明公式对，
只能证明它在那组输入上对。**

2026-08-01 阶段 3 验收时才暴露：噬尸蛞蝓吃掉同伴获得 `StrengthPower 4` 后，
`/state` 仍报 `3×2`，实际掉血却是 `(3+4)×2 − 3 格挡 = 11`。

正确来源是 `AttackIntent.GetSingleDamage(targets, owner)` /
`GetTotalDamage(...)` —— 它们内部再走一遍 `Hook.ModifyDamage`，
那才是游戏画在意图上的数字。详见 `game-model.md`。

**教训**：验证用例必须覆盖「修正生效」的情形。全零修正下的吻合是假阳性。
这条 bug 的危害是单向的 —— 永远只会低估敌人伤害，且敌人越强低估越多。

### 阶段 3 · 动作执行（C#）

实现在 `src/Sts2Bridge/ActionApi.cs`，接口为 `POST /action/<动作>`。

- [x] 3.1 `play_card(card, target?)` → `CardModel.TryManualPlay(target)`
- [x] 3.2 `end_turn()` → `PlayerCmd.EndTurn(player, false, null)`
- [x] 3.3 `use_potion(slot, target?)` → `PotionModel.EnqueueManualUse(target)`
      **成功路径已实机验证**（2026-08-01 精英战：肌肉药水 → 力量 +5，
      `target` 正确回报为 `player`，即当初复刻的「目标为空时指向自己」那段兜底）
      （**成功路径尚未实机验证** —— 验证时身上没有药水，只验了空槽驳回）
- [x] 3.4a **选牌应答**（`POST /action/choose`）—— 见下方「3.4a 结论」。
      归类更正：选牌**不是**非战斗场景，是**战斗内刚需** ——
      静默猎手起手牌组里「生存者」「早有准备」都带弃牌，不接管就寸步难行
- [x] 3.4b **地图移动**（`POST /action/move`）—— 见下方「3.4b 结论」
- [x] 3.4c **战斗奖励与卡牌三选一**（`POST /action/pick` + `/action/proceed`）
      —— 见下方「3.4c 结论」
- [x] 3.4d **休息点与宝箱** —— 已实机验证（2026-08-01）：
      宝箱 `pick[0] Whetstone` 开箱拿到遗物、`proceed` 离开；
      休息点 `pick[0] HEAL` 烤火，HP 44→68/80，`proceed` 离开。
      要点：这两个不是覆盖界面而是**房间节点**（`RoomContainer` 下），
      故「上下文」从界面扩展到房间，`pick` / `proceed` 统一覆盖。
      休息点选项的可用性挂在 `Option.IsEnabled` 上而非按钮上；
      宝箱分两步（`_hasChestBeenOpened` 之前只能开箱，之后才有遗物可拿）
- [x] 3.4e **事件房** —— 靠通用兜底分支自动打通，并额外读出**选项文本**：
      节点名不总是有语义（实测事件按钮叫 `@Control@1132` 这种 Godot 自动生成
      的名字），而事件恰恰最依赖文本 ——「失去 34 金币，获得 2 瓶随机药水」和
      「失去 9 点生命，选择一张牌变化」只能靠读文本区分。
      故兜底分支会从按钮的后代里找第一个非空 `Text`，剥掉 BBCode 后截断到 80 字。
      实测事件生效：金币 181→147、药水到手
- [x] 3.4f **商店** —— 槽位带价格与 `EnoughGold`，点 `Hitbox` 购买；**待实机验证**。
      2026-08-01 曾专门去找：一路把 `Shop` 排在路线偏好第一位，但该局地图从
      所在位置起就没有可达的商店节点。属地图随机性，非实现问题
- [ ] 3.4g 商店的除卡服务、Boss 遗物三选一（预期已被通用兜底覆盖，待验证）。
      Boss 遗物**必须打赢 Boss 才会出现** —— 2026-08-01 打到了第一章 Boss
      `CeremonialBeast`（252 血），但基础牌组每回合仅约 14 点伤害、而它每回合
      打 22 且力量持续增长，数值上不可能赢。**验证它的前提是先有一套能打过
      Boss 的牌组**，那是阶段 6 的事，不是「多走几步」能解决的
      **不必再从零测绘**，两条现成的路（详见 `game-model.md`）：
      - **选牌一律走 `CardSelectCmd.UseSelector(ICardSelector)`** —— 官方注入点，
        弃牌/检索/除卡/升级/转化/卡牌奖励/三选一全部收口于此，`options` 与
        `minSelect/maxSelect` 正是要回报给模型的东西。用 BCL 的
        `DispatchProxy` 在运行时实现该接口即可
      - 地图、商店按钮、事件选项这类非选牌交互才退回 UI 点击
        （`UiHelper.FindAll<T>` 找节点再 `UiHelper.Click`，AutoSlay 就这么干）
      - 阻塞式选择的应答也一并解决 3.7 那条「有未决选择时动作会被取消」
- [x] 3.5 **就绪判据**：轮询至队列空、执行器停、`!IsExecutingCardOrPotionEffect`
      且 `Phase == Play`（或战斗已结束）方返回。见下方「3.5 结论」
- [x] 3.6 错误回传：以 `CanPlay` / `IsValidTarget` 预检，非法动作返回
      HTTP 200 + `ok:false` + 结构化 error/reason

#### 3.1–3.2 实机验证（2026-08-01，v0.107.1，静默猎手 A6 第 2 层）

```
出牌   POST /action/play_card?card=3&target=0    settled=true  379 ms
       energy 3→2   手牌 7→6   弃牌 0→1   怪0 血 27→21
结束回合 POST /action/end_turn                    settled=true  6577 ms
       回合 1→2   阶段 Play   energy →3   我方 HP 56→42
```

**能量确实被扣掉**，这是「走的是玩家出牌路径而非免费的 `AutoPlay`」的判据。
HP 恰好 −14 = 预告的 `3×2` + `8`，与 `/state` 导出的意图完全吻合。
结束回合等了 6.6 秒才返回 —— 它确实把整个敌方回合走完了。

驳回路径六条均已实测：`bad_target`（该给目标不给 / 不该给却给）、
`bad_index`（手牌越界 / 目标越界）、`empty_slot`、未知动作与 GET 动作各转 400。

#### 3.5 结论：还有第三种「没结束」—— 停下来等玩家选择

最初的就绪判据只有两种归宿：稳定，或超时。实测打出「求生者」
（获得格挡并弃一张牌）时出现了第三种：动作跑到一半停住，**等玩家选弃哪张牌**，
于是白等满 20 秒才报一个语焉不详的 `settled:false`。

判据是动作自己的状态，不是队列：

```
RunManager.Instance.ActionQueueSet.IsEmpty                    = false
RunManager.Instance.ActionExecutor.IsRunning                  = false   ← 分不清
RunManager.Instance.ActionExecutor.CurrentlyRunningAction.State
                                              = GatheringPlayerChoice   ★
CombatManager.Instance.PlayerActionsDisabled                  = false
PlayerCombatState.Phase                                       = Play
```

「队列非空 + 执行器不在跑」既可能是刚入队还没轮到，也可能是停在等选择上，
两者靠 `GameAction.State` 才分得开。现已改为立即返回
`awaiting_choice:true` 并附上栈顶界面类型（`NOverlayStack.Instance.Peek()`
的类型名，正是 AutoSlay 分派界面处理器所用的键），交由阶段 3.4 应答。

需持续 500 ms 才判定 —— 游戏内部也会短暂进入该状态再自行走完。

#### 3.5 复验（2026-08-01，重启后，第 4 层 SludgeSpinnerWeak）

四条修复全部实机通过：

| 验的是什么 | 结果 |
|---|---|
| 战斗外血量（2.7） | 地图/结算界面 `/state` 给出 `player.hp = 31/70` |
| 目标标识 | `target` 为 `SludgeSpinner`，不再是 `Creature` |
| `awaiting_choice` | 打「求生者」**788 ms 返回**，旧行为是干等 20000 ms |
| 意图伤害（2.6） | 「中和」挂上虚弱 1 层后，意图 `8 → 6`（8×0.75）。
若仍走 `DamageCalc` 会纹丝不动停在 8 |

意图那条的判别设计值得留意：**不必等运气碰上带力量的敌人** ——
自己打一张给虚弱的牌，同样能让完整伤害管线与基础值产生分歧，
且效果立即可见。找一个能让两条路径give出不同数字的最短输入，比等特定局面快得多。

#### 3.7 新发现：选择未决时入队的动作会被**取消**（已修）

复验途中撞上的：「求生者」的弃牌选择未决时下发「中和」，桥接层报了
`ok:true`，而牌留在手里、敌人毫发无损、队列随后变空。

根因是 `PlayCardAction` 重写的 `CancelAction` —— 注释直言弹出手牌选择界面
需要取消已排队的出牌。**报成功而实际没发生，是所有错误里最坏的一种**：
上层会照着一个从未生效的动作继续往下推。

已在 `ActionApi.Begin` 加前置检查，有未决选择时直接返回
`error:"awaiting_choice"`，并把该状态一并加进 `/state`
（见下方 2.8）—— 待实机验证。

##### 踩坑：选择期间手牌张数不变，别拿它推断选择是否完成

弃牌要确认后才生效，选择期间 `/state` 的手牌张数、能量、格挡全都已是终值。
当时据此判断「选择已解掉」，于是把一次被取消的出牌误读成 bug 现场，
多绕了一圈。**「在等玩家选择」只能看 `GameAction.State`，不能从状态数字反推**
—— 这也正是要把它加进 `/state` 的理由：不明说，上层就只能猜，而猜必然会错。

#### 3.4a 结论：走 `CardSelectCmd.UseSelector`，不点 UI

游戏把一切选牌收口到了一个可替换的接口，每个调用点都是同一形状：

```csharp
if (Selector != null) result = await Selector.GetSelectedCards(options, min, max);
else                  …弹界面，等玩家点…
```

装上自己的 `ICardSelector` 即全部接管。新增 `POST /action/choose?cards=0,2`，
`/state` 多出 `choice` 字段（选项、min/max）。

**卡牌奖励不受影响**：接口另一个方法 `GetSelectedCardReward` 只在
`_currentlyShownScreen == null` 时才被问到，正常游玩时三选一仍走 UI。
注意它的返回类型是 **struct**，返回 null 会炸。

实机验证（2026-08-01）：

```
打出求生者 → awaiting_choice, 708 ms, 格挡 +8
/state     → choice: 6 选 1，逐项给出 id/cost/type
应答前打别的牌 → 被拒 (awaiting_choice)
choose [1] → 88 ms，手牌 6→5，弃牌堆 2
再打一张打击 → 正常命中，怪 39→33
```

##### 踩坑：DispatchProxy 的三条要求，全都只在运行时报错

桥接层不能编译期实现游戏的接口，只能用 `DispatchProxy` 运行时生成。
连踩三次：

| 现象 | 原因 |
|---|---|
| `AmbiguousMatchException: Create[T,TProxy]()` | .NET 9 起 `Create` 有泛型与非泛型两个重载，`GetMethod("Create", …)` 无从区分。改为遍历 `GetMethods` 按签名挑 —— 非泛型的 `Create(Type, Type)` 恰好正对我们「接口类型运行时才知道」的场景 |
| `The base type … cannot be sealed` | DispatchProxy 是**继承**代理类型来生成实现的，故不能 sealed |
| 可访问性冲突 | 生成的代理位于另一个动态程序集，代理类型必须是 **public 顶层类型**，不能是 internal 或嵌套 |

##### 线程：应答必须回到主线程

`TaskCompletionSource` 的后续会在**完成它的那个线程上就地执行**，
而这里的后续是游戏的战斗逻辑。在 HTTP 线程 `SetResult`，等于把整个战斗
搬离主线程。故 `Resolve` 由 `MainThread.RunSync` 调度。

##### 取舍：接管即绕过 UI

装上选择器后选牌界面不再弹出，手动游玩时点不了。故由
`STS2MCP_CHOICE=1` 控制（两个启动器默认开启，清掉即恢复原样），
并留了 180 秒兜底：无人应答时取前 min 张放行并大声记日志 ——
**硬挂起比自动替玩家决定更糟**，玩家还无从得知原因，因为 UI 已被绕过。

#### 3.4b 结论：地图同样不必碰 UI

原以为非战斗一律得照 AutoSlay 点 Godot 节点，实际不必。玩家点完地图节点后，
游戏走的是一条纯模型层的路：

```csharp
// MapSelectionSynchronizer.MoveToMapCoord()
var action = new MoveToMapCoordAction(LocalContext.GetMe(runState), coord);
actionQueueSynchronizer.RequestEnqueue(action);
```

**与出牌完全同形** —— 构造 GameAction，走同一个入队通道，动作内部再去驱动
地图动画与 `RunManager.EnterMapCoord`。桥接层至此仍未调用任何 Godot 原生 API。

`/state` 增加 `map` 段（当前坐标、`can_move`、可走节点的下标/行列/房间类型），
`POST /action/move?node=<下标>` 执行移动。

##### 踩坑：地图是「界面」不是「房间」

初版用 `CurrentRoom is MapRoom` 判断能否移动，**实测恒为 false**。
打完一个房间后地图界面浮出来，而 `CurrentRoom` 仍停在刚打完的那个
（实测停在 `EventRoom` 且 `IsPreFinished = true`）。`MapRoom` 只用于特定流程。

正确判据是 `NMapScreen.Instance.IsOpen && IsTravelEnabled` —— 后者正是游戏
自己用来控制「此刻能不能点节点」的开关。两个都是纯托管自动属性，
读它们不触碰 Godot 原生侧。

##### 踩坑：坐标先于房间就位

移动的完成判据一开始只判「走到了目标坐标」，返回的却是
`room=null, in_combat=false` 的快照 —— 紧接着的一次调用就报「当前房间
CombatRoom」，即房间是在我们返回之后才装配起来的。

现在要过三道关：坐标到位 → 房间已建好 → 若是战斗房还要等开打并进入出牌阶段。
实测耗时从 1060 ms 变成 5044 ms，多出来的正是房间装配与开局发牌。

这与 3.5 是同一类错误：**拿一个早于目标事件的信号当完成判据**。
队列空不等于效果跑完，坐标到位不等于房间就绪。

##### 踩坑：`Children` 是 HashSet，枚举顺序不保证稳定

模型读到一份选项、再按下标移动，两次枚举顺序不同就会走错节点。
故一律按 `(row, col)` 排序，且导出与执行**共用同一个入口** `MapNav.Options()`。

#### 3.x 实战演练（2026-08-01）

经 MCP 连打两场，全程未碰鼠标：

```
场次一 ToadpolesWeak   4 回合   HP 56→49（只掉第 1 回合那 7 点）
场次二 三只史莱姆      4 回合   HP 56→45
```

几个决策用上了导出的信息，值得记录 —— 它们是「状态导出到底够不够用」的实证：

- **精确斩杀**：怪 21 血，`打击×3 + 中和` 正好 21，一点不浪费
- **意图为 Buff 时不叠格挡**：格挡到下回合清零，全砸输出
- **顺序换一下**：怪物拿到 `ThornsPower2`（打它反弹 2），故先叠满 13 格挡再攻击，
  反弹被格挡吃掉，血一点没掉
- **弃牌弃诅咒**：`choose` 把 `AscendersBane` 丢进弃牌堆
- **虚弱降低意图**：中和挂上虚弱后意图 `11 → 8`，实战复验了 2.6 的伤害管线修复

另有一次真实的操作失误：沿用了过期的手牌下标，被 `bad_index` 拦下，
游戏状态一点没动 —— 结构化错误在人也会犯的错上起了作用。

#### 3.4c 结论：这一步确实得点 UI，但代价很小

领奖没有可用的纯模型层入口。核心 `RewardsSetSynchronizer.SelectLocalReward(reward)`
确实是模型层的，但只调它会留下一个已领却仍在界面上的按钮，「继续」也不解锁 ——
整套语义收口在按钮上：

```csharp
NRewardButton.OnRelease() → GetReward()
    Disable();
    if (await RunManager.Instance.RewardsSetSynchronizer.SelectLocalReward(Reward))
    { …飞入动画…; EmitSignal(RewardClaimed, this); }   ← 界面靠这个移除按钮
```

而「点击」的成本极低：AutoSlay 的 `UiHelper.Click` 全部实现就是
`button.ForceClick()`，一个托管方法调用。

##### 澄清：调用 Godot API 并不危险，危险的是编译期引用

此前把「碰 Godot」整体列为高危，**夸大了**。当初让游戏 10 毫秒硬崩溃的是
**编译期引用 GodotSharp** —— 它使该程序集在 Default ALC 中被重复加载，
形成两套类型标识。反射调用取到的是游戏 ALC 里**已经存在**的那一份，
不存在重复加载。真正的约束只有一条：**必须在主线程**。

##### 两种界面，两种点法

| 界面 | 选项节点 | 怎么点 |
|---|---|---|
| `NRewardsScreen` | `NRewardButton`（`NClickableControl`） | `ForceClick()` |
| `NCardRewardSelectionScreen` | `NGridCardHolder`（继承 `Godot.Control`，**没有** `ForceClick`） | 直接调界面的私有方法 `SelectCard(holder)` —— 它正是该节点 `Pressed` 信号的接收端 |

卡牌三选一**不走选牌选择器**：`CardReward` 的代码里 `_currentlyShownScreen != null`
时压根不问 `CardSelectCmd.Selector`，所以 3.4a 的接管管不到它。

对外统一成 `/state.screen`（`type` + `options[].i` + `can_proceed`）与
`pick(i)` 一个动作 —— 模型只需记一个动作，而不是每种界面一个。
不认识的界面报出类型名与空 `options`，让模型知道「卡在一个处理不了的界面上」。

##### 踩坑：等待判据被「战斗已结束」这条捷径吃掉

领卡牌奖励只等了 121 ms 就返回，此时三选一界面还没弹出来，模型看到的是一份
「什么都没发生」的状态。原因是等待循环里 `if (!InCombat) return settled;`
排在最短观察时间**之前** —— 而领奖、按继续都发生在战斗外，那段观察窗口
根本轮不到生效。界面点击现已单独处理并排在该捷径之前，实测稳定在 ~710 ms。

##### 踩坑：界面按完「继续」仍留在覆盖层栈上

回到地图后 `ScreenCount` 依然是 1，只是界面已不可见。只看栈会报出一个
空选项却又 `can_proceed=true` 的界面，而 `map.can_move` 已是 true ——
模型会以为还得再按一次继续。判据改为 `IsVisibleInTree()`。

#### 3.x 完整循环实证（2026-08-01）

```
领金币      99 → 112                          708 ms
领药水      FruitJuice 入袋                   723 ms
点卡牌奖励  → NCardRewardSelectionScreen       708 ms
            PreciseCut / HandTrick / CalculatedGamble
选 PreciseCut → 回到奖励界面                   723 ms
proceed     → can_move=true，可走节点 (2,1)Monster  713 ms
```

至此「战斗 → 奖励 → 三选一 → 继续 → 地图 → 下一场」全链路无需碰鼠标。
一整局里仍需人工介入的只剩商店、事件、休息点、宝箱。

#### 3.4d 结论：界面之外还有「房间」这一层

休息点、宝箱不是覆盖界面，而是场景树里的**房间节点**：

```
/root/Game/RootSceneContainer/Run/RoomContainer/<CombatRoom|RestSiteRoom|TreasureRoom|…>
```

只盯覆盖层栈会完全看不见它们。对外仍统一成一个 `screen` 段与一个 `pick`
动作 —— 模型不必区分「这是弹窗还是房间」。

新增 `GET /tree?path=...&depth=N` 转储场景树。**这是必需品**：界面工作的
难点全在「那个节点叫什么、在哪一层」，而节点名来自 `.tscn` 场景文件，
**不在程序集里，反编译也看不到**。靠它两次就定位到了 RoomContainer。

##### 踩坑：残留界面与地图会同时「成立」

按下「继续」回到地图后，奖励界面**仍留在覆盖层栈上、且 `IsVisibleInTree`
仍为 true**（只有「继续」按钮变灰了）。仅凭可见性挡不住，模型会看到一份
「既能走地图、又有三个奖励可领」的自相矛盾状态。

改用语义互斥判定：**地图可走 = 游戏在等你选路**，此时任何残留界面都不该抢镜。
两件事不可能同时成立，用互斥关系比猜界面可见性可靠得多。

### 阶段 4 · 传输层（C#）

- [x] 4.1 游戏内 HTTP on `127.0.0.1:8765`（**仅绑 loopback**）：
      `GET /health` `/state` `/glossary` `/eval` `/describe`、
      `POST /action/<动作>`。
      **未用 `HttpListener`** —— 它依赖 HTTP.sys，普通权限进程绑定端口前缀
      常需 netsh 注册 urlacl，而游戏由 Steam 以普通权限启动，不能要求用户
      额外配置。改用 `TcpListener` 自行实现 GET/POST + JSON 的最小子集。
- [x] 4.2 请求 → 主线程队列 → `TaskCompletionSource` 取回结果
- [x] 4.3 异常兜底：任何异常均不得导致游戏进程崩溃

### 阶段 5 · MCP Server（Python）

实现在 `src/mcp_server/server.py`（单文件，约 200 行，刻意做得很薄）。

- [x] 5.1 依赖：`mcp>=2.0`、`httpx`
- [x] 5.2 stdio 传输的 MCP server 骨架
- [x] 5.3 工具：`get_state` / `get_glossary` / `play_card` / `end_turn` /
      `use_potion` / `health`
- [ ] 5.4 **`auto_play_until(stop_on=[...])`** —— 全自动的核心。内部循环：读状态 → 唯一合法动作则本地执行 → 命中停止条件（卡牌奖励/地图/Boss/精英/低血量/死亡）才返回。
      **未实现** —— 它的价值取决于阶段 3.4：非战斗场景尚不能自动应答，
      循环跑到第一个卡牌奖励就会停住，先做它收益有限
- [x] 5.5 工具 description 的 prompt 工程
- [x] 5.6 注册到 Claude Code（`.mcp.json`）
- [~] 5.7 ~~用 MCP Inspector 调试~~ —— **作废**。已用 `mcp` 自带的 `Client`
      走完整 stdio 协议自测（见下），可脚本化、可回归，比 Inspector 更合用

#### 5.3 取舍：`get_legal_actions` 与 `choose` 未实现

- `get_legal_actions` 被折叠进 `get_state`：`hand[].playable` 与失败时的
  `reason` 已经给出全部信息。单独开一个工具意味着同一份事实有两个来源，
  且第二份会因为下标重排而过期 —— 多一个工具就多一份说明书要写、
  多一处可能不一致。
- `choose` 依赖阶段 3.4 的选择应答机制，那边没做完，这边无从实现。
  目前遇到 `awaiting_choice` 只能由人来点。

#### 5.2 结论：`mcp` 2.0 的 API 与旧版不同

`FastMCP` 已更名为 `MCPServer`，且不在 `mcp.server.fastmcp` 下：

```python
from mcp.server.mcpserver import MCPServer   # 不是 FastMCP
server = MCPServer(name="sts2", instructions="…")

@server.tool(description="…")
def get_state() -> dict: ...

server.run("stdio")
```

自测客户端侧：`Client` 收的是 Transport 而非 `StdioServerParameters`，
须传 `stdio_client(params)`；工具的 schema 字段是 `input_schema`（非 `inputSchema`）。

#### 5.x 实机自测（2026-08-01）

用 `mcp` 自带的 `Client` 以 stdio 拉起本 server，走完整协议：

```
注册工具 6 个，schema 正常
health        → attached=true
get_state     → in_combat=true, hp=31/70, 手牌 7 张
get_glossary  → 卡牌 11 条 / 遗物 2 条；第二次命中缓存
play_card 99  → ok=false, error=bad_index（游戏状态未变）
play_card 求生者 → ok=true, awaiting_choice=true, 746 ms
  ↳ /state 的 awaiting_choice 同步为 true
  ↳ 此时再下发打击 → ok=false, error=awaiting_choice
  ↳ **那张打击原封不动还在手里** —— 没有假装成功（对比 3.7 修复前）
```

#### 踩坑：读超时必须大于桥接层的等待上限

`end_turn` 要等完整个敌方回合（实测 5~8 秒，多怪更久），桥接层默认等到
20 秒。客户端若先超时，动作其实**已经下发**，模型却收到一个失败 ——
它会重发，于是多结束了一个回合。故 httpx 的 read timeout 设为 120 秒，
且超时文案明确要求「先 get_state 确认，不要重发」。

### 阶段 6 · 让它打得好

- [ ] 6.1 三档决策分流（见下）
- [ ] 6.2 战斗启发式：能斩杀则斩杀；否则按「本回合总伤害 vs 所需格挡」排序。
      **原型已写过一版并打死了一整局**，教训见下方「6.2 血的教训」
- [x] 6.3a **策略沉淀** → `docs/strategy.md`。只收录实战验证过的判断，
      且每条都注明依赖 `/state` 的哪个字段 —— 策略与状态导出是一体的，
      导不出的信息就是做不出的决策
- [ ] 6.3b 把 strategy.md 提炼成决策 prompt 模板
- [ ] 6.4 整局 runner（Claude Code 交互式跑不完一整局）
- [x] 6.4b **实时卡牌伤害** → `hand[].values` 与 `hand[].damage_vs`。
      卡面文本不含力量/虚弱/易伤等修正，拿它算斩杀线会算错 —— 2026-08-01
      Boss 战最后一回合因此差 5 点没触发击晕而阵亡，详见 strategy.md §4。
      入口不是 `GetDescriptionForPile`（那条线索是错的，见下方「6.4b 结论」），
      而是 `CardModel.DynamicVars` + `UpdateDynamicVarPreview`
- [ ] 6.4c **待选卡牌的卡面文本**：卡牌三选一 / 商店 / Boss 遗物给出的只有
      标识（`Anger` / `Colossus` / `Spite`），`/glossary` 又只覆盖**已持有**的
      牌与遗物（`Deck.Cards` + `PlayerCombatState.AllCards`），于是**模型是在
      看不见效果的情况下选牌的**。strategy.md §5 刚得出「构筑决策决定一局上限」，
      却恰恰卡在这里。2026-08-01 实机撞上：只能靠对 StS1 的印象猜 `Anger` 是什么。
      修法：让 `/glossary`（或 `screen.options[]` 自身）把当前奖励界面上的
      候选卡也渲染进去
- [ ] 6.5 决策日志 `(state, action, 理由)` —— 唯一能让它变强的东西
- [x] 6.6a 死亡后可脱身：游戏结束界面 → 主菜单 → 开新局，全程经 `pick`
      （靠通用兜底分支实现，见下方「6.6 结论」）
- [ ] 6.6b 死亡/胜利的**自动**检测与开新局（`run.game_over` 已导出，尚未自动化）

#### 6.2 血的教训：一个「朴素」原型打死了一整局（2026-08-01）

为了快速推进到休息点，写了个战斗启发式原型代打，在 HP 37/70、面对四只史莱姆
时把方向盘交给了它。结果：

```
回合2  来袭=26  HP=33  → 判定「优先格挡」，却先打了一张打击、只叠 1 张防御，
                         还剩 1 点能量没用  → 掉 21 血
回合4  来袭=22  HP=12  → 打了两张 Slimed（史莱姆塞进来的废牌，毫无作用）
                         能量耗尽             → 死
```

三个缺陷，写下来给下一版当硬性要求：

| 缺陷 | 要求 |
|---|---|
| 「优先格挡」只是排序偏好，攻击牌照样排在前面被打出 | **格挡必须是硬约束**：算出所需格挡量，先满足它，剩余能量才用于输出 |
| 打出了 `Slimed` 这类废牌（可打出 ≠ 值得打） | **显式白名单/评分**，不可打出的判据是 `playable`，值不值得打是另一回事 |
| 回合结束时还剩能量 | 结束回合前必须确认无更优出牌 |

**更根本的错误在分层上**：§4 写明第 3 档（高价值决策）应当询问模型，而
「37/70 血面对四只怪」正属于此列。图省事让本地启发式接管，代价是一整局。

**规则：本地启发式只允许在安全线以上接管**（如预计掉血 < 当前 HP 的 1/4
且无致死风险），一旦接近危险就必须上交模型。这条比启发式本身更重要 ——
启发式写得再好也会有盲区，分层是兜底。

##### v2 实测：三条要求全部生效，安全线也确实兜住了

照上表重写后连打四场，无一次濒危：

```
FuzzyWurmCrawler 55 血  全程只掉 6 血（第 4 回合连叠三张防御到 15 格挡挡住 11 点）
Nibbit 45 血            HP 64→58
ShrinkerBeetle 39 血    HP 58→55，战后 BurningBlood 回到 61
双怪 31+49 血           ⚠ HP 53/80、来袭 26 > 血量 1/4 → **停手交还**
```

最后那次交还是关键：接手后看一眼就知道该斩杀而非堆格挡 —— 怪 1 只剩 16 血
却要打 16 点，两张打击加愤怒正好 18 点弄死它，那 16 点伤害直接消失；
若照 v2 的「先堆格挡」打，10 点格挡挡不住 26，要白掉 16 血。
**这正是分层的意义：启发式管常规，模型管拐点。**

#### 6.4b 结论：卡面文本这条线索是错的，数值入口在 DynamicVars

任务原本记的线索是「`GetDescriptionForPile` 输出的是已代入数值的最终文本」。
**这句话本身就是那次阵亡的成因**：该方法只是用格式串去读
`DynamicVar.PreviewValue`，而 `PreviewValue` 需要先调
`UpdateDynamicVarPreview` 才是修正后的数字，否则等于卡面裸值。游戏界面每次
刷新卡面都会先算一遍，所以玩家看到的是对的；我们直接调渲染就拿到了裸值。
完整链路见 `game-model.md` 的「卡牌的实时数值 DynamicVars」。

`/state` 现在照界面的做法走同一条管线（`ClearPreview` → `UpdateDynamicVarPreview`
→ 读 `PreviewValue`），产出两个字段：

```jsonc
{"i":0,"id":"Bash","cost":2,"values":{"Damage":8,"VulnerablePower":2}}
{"i":1,"id":"StrikeIronclad","cost":1,"values":{"Damage":6},"damage_vs":[9,6]}
```

- `values` 装自身侧修正（力量、虚弱化、遗物、附魔），与目标无关
- `damage_vs` 装目标侧修正（易伤），逐敌各算一遍，**只在确实有差异时才发** ——
  常态下一个字节都不多花

##### 实机验证（2026-08-01，力士，第 2 层双尸蛞蝓）

```
虚弱化 2 层的防御   卡面 5 → values.Block=3      打出后 player.block 确实是 3
易伤 2 层的目标     卡面 6 → damage_vs=[9,6]     打出后敌人 17 → 8，正好 9
虚弱化过期后        →      values.Block 自动回到 5
一击斩杀            敌人 9 血，damage_vs=[9]     一张打击结束战斗
```

三处踩坑固化在代码注释里：

| 坑 | 处理 |
|---|---|
| 取整是**截断**不是四舍五入（`5×0.75=3.75→3`） | 导出侧同样截断，否则每张牌多报 1 点 |
| `PreviewValue` 是共享状态，界面读的是同一份 | 算完逐敌之后**复原成无目标预览**，别让手牌停在针对最后一只怪的数字上 |
| 每张牌逐敌算一遍不便宜 | 只有 `values` 里真有 `Damage` 键时才逐敌算；技能牌一遍都不多跑 |

#### 6.6 结论：认不出的界面不该等于死路

死亡当时 `NGameOverScreen` 报了 0 个选项、`can_proceed` 也是 false，
桥接层既检测不了死亡、也退不出那个界面 —— 整条链路彻底卡死。

修法不是给游戏结束界面写一个专用处理器，而是给 `Screens.OptionsOf` 加**兜底
分支**：认不出的界面就把所有**可见且启用**的 `NClickableControl` 后代按
**节点名**列出来。节点名本身有语义（`Continue` / `SingleplayerButton` /
`ConfirmButton`），模型看得懂。

一次改动顺带打通了游戏结束界面、主菜单、角色选择、**以及事件房**（它的四个
选项也自动出现了）。同时把主菜单纳入 `Context()` —— 没有房间时（未进局）
以 `Game/RootSceneContainer/MainMenu` 为上下文，否则死亡之后无路可走。

实测：从游戏结束 → 主菜单 → 单人 → 标准 → 角色选择 → 确认，全程经 `pick` 完成。

##### 遗留：点角色按钮不生效

`ForceClick` 点 `SILENT_button` 后确认，开出来的却是 Ironclad。AutoSlay 用的
是 `NCharacterSelectButton.Select()` 而非点击 —— 该按钮的选中逻辑不在
`Released` 信号上。需要专门处理，暂记。

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
