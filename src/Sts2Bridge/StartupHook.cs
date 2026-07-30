using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;

/// <summary>
/// ============================================================================
///  【已废弃 —— 本类不会被调用，保留仅作为失败方案的记录】
///
///  实测结论：`DOTNET_STARTUP_HOOKS` 对本游戏完全无效。
///
///  CoreCLR 的 startup hook 由 StartupHookProvider.ProcessStartupHooks() 在
///  `coreclr_execute_assembly` 的执行路径中触发；而 Godot 使用 hostfxr 的
///  `load_assembly_and_get_function_pointer` 加载托管代码，从不调用该函数，
///  故整段代码路径不可达 —— 属性来自环境变量还是 runtimeconfig.json 均无差别
///  （两种途径均已实测失败）。
///
///  现行方案：CoreCLR Profiler（`src/Sts2Profiler/`）在 CLR 初始化最早期加载，
///  向 `NGame..cctor` 注入 IL 以载入桥接层。实际入口见 `Entry.cs`。
///
///  详见 docs/spec.md 的 1.2 结论。
/// ============================================================================
///
/// .NET 运行时启动钩子入口。
///
/// 硬性契约（由 CoreCLR 强制，不可更改）：
///   - 类名必须为 StartupHook
///   - 必须位于全局命名空间（不能有 namespace）
///   - 必须有 static void Initialize() 方法
///
/// 由环境变量 DOTNET_STARTUP_HOOKS 指向本程序集时，CoreCLR 会在游戏的
/// Main 之前加载并调用 Initialize()。
///
/// 【阶段 1.2 验证专用版本】
/// 本版本刻意做到零外部依赖 —— 只使用 BCL，不引用 sts2.dll，不引用
/// 0Harmony.dll。原因：若 hook 引用了外部程序集而加载失败，将无法区分
/// 「hook 机制不工作」与「依赖解析失败」这两种截然不同的故障。
/// 此步骤只验证一件事：CoreCLR 到底有没有执行我们的代码。
/// </summary>
internal static class StartupHook
{
    // 日志写绝对路径。进程的工作目录是游戏目录，相对路径会写到那里去，
    // 既污染游戏文件夹，也不好找。
    private static readonly string LogPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
        "sts2-mcp", "logs", "inject-test.log");

    public static void Initialize()
    {
        // 整个 hook 用 try/catch 包死。任何异常都不能影响游戏启动 ——
        // startup hook 抛出的异常会导致宿主进程直接终止。
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(LogPath)!);

            var sb = new StringBuilder();
            sb.AppendLine();
            sb.AppendLine("================================================================");
            sb.AppendLine($"  注入成功  INJECTED  {DateTime.Now:yyyy-MM-dd HH:mm:ss.fff}");
            sb.AppendLine("================================================================");

            var proc = Process.GetCurrentProcess();
            sb.AppendLine($"进程          : {proc.ProcessName} (PID {proc.Id})");
            sb.AppendLine($"主模块        : {SafeGet(() => proc.MainModule?.FileName)}");
            sb.AppendLine($"CLR 版本      : {Environment.Version}");
            sb.AppendLine($"运行时描述    : {System.Runtime.InteropServices.RuntimeInformation.FrameworkDescription}");
            sb.AppendLine($"BaseDirectory : {AppContext.BaseDirectory}");
            sb.AppendLine($"工作目录      : {Environment.CurrentDirectory}");
            sb.AppendLine($"命令行        : {Environment.CommandLine}");
            sb.AppendLine($"STARTUP_HOOKS : {Environment.GetEnvironmentVariable("DOTNET_STARTUP_HOOKS")}");
            sb.AppendLine($"本程序集位置  : {SafeGet(() => typeof(StartupHook).Assembly.Location)}");

            var loaded = AppDomain.CurrentDomain.GetAssemblies();
            sb.AppendLine($"此刻已加载程序集: {loaded.Length} 个");
            sb.AppendLine("  (Main 尚未执行，游戏程序集大概率还没加载 —— 这是预期的)");

            Write(sb.ToString());

            // 第二次探测：起一个后台线程，等游戏跑起来后再看一次。
            // 目的是回答第二个问题：我们和游戏是否处于同一个
            // AssemblyLoadContext —— 能否「看见」sts2 程序集。
            // 这决定了后续能否用 Harmony 打补丁。
            var t = new Thread(ProbeLater) { IsBackground = true, Name = "sts2-mcp-probe" };
            t.Start();
        }
        catch (Exception ex)
        {
            TryWriteFallback(ex);
        }
    }

    /// <summary>游戏启动 12 秒后再次探测，检查能否看到游戏程序集。</summary>
    private static void ProbeLater()
    {
        try
        {
            Thread.Sleep(12_000);

            var loaded = AppDomain.CurrentDomain.GetAssemblies();
            var sb = new StringBuilder();
            sb.AppendLine();
            sb.AppendLine("---------------- 12 秒后二次探测 ----------------");
            sb.AppendLine($"已加载程序集: {loaded.Length} 个");

            // 只关心这三个：游戏本体、Godot 绑定、Harmony
            foreach (var key in new[] { "sts2", "GodotSharp", "0Harmony" })
            {
                var hit = loaded.FirstOrDefault(a =>
                    string.Equals(a.GetName().Name, key, StringComparison.OrdinalIgnoreCase));

                sb.AppendLine(hit != null
                    ? $"  [✓] {key,-12} 已加载  v{hit.GetName().Version}  {SafeGet(() => hit.Location)}"
                    : $"  [✗] {key,-12} 未找到");
            }

            // 若能看到 sts2，顺手确认几个关键类型是否可反射到 ——
            // 这直接预示阶段 1.5 和 2.x 是否可行。
            var sts2 = loaded.FirstOrDefault(a =>
                string.Equals(a.GetName().Name, "sts2", StringComparison.OrdinalIgnoreCase));

            if (sts2 != null)
            {
                sb.AppendLine();
                sb.AppendLine("关键类型可达性检查:");
                foreach (var typeName in new[]
                {
                    "MegaCrit.Sts2.Core.Combat.CombatManager",
                    "MegaCrit.Sts2.Core.Runs.RunManager",
                    "MegaCrit.Sts2.Core.Commands.CardCmd",
                    "MegaCrit.Sts2.Core.Commands.PlayerCmd",
                    "MegaCrit.Sts2.Core.Models.CardModel",
                    "MegaCrit.Sts2.Core.Saves.JsonSerializationUtility",
                })
                {
                    var t = SafeGet(() => sts2.GetType(typeName)?.FullName);
                    sb.AppendLine(t != null ? $"  [✓] {typeName}" : $"  [✗] {typeName}");
                }
            }

            sb.AppendLine("------------------------------------------------");
            Write(sb.ToString());
        }
        catch (Exception ex)
        {
            TryWriteFallback(ex);
        }
    }

    private static void Write(string text)
    {
        File.AppendAllText(LogPath, text, Encoding.UTF8);
    }

    private static T? SafeGet<T>(Func<T?> f)
    {
        try { return f(); } catch { return default; }
    }

    /// <summary>连日志都写不了时的兜底，尽量留下痕迹。</summary>
    private static void TryWriteFallback(Exception ex)
    {
        try
        {
            var p = Path.Combine(Path.GetTempPath(), "sts2-mcp-hook-error.log");
            File.AppendAllText(p, $"[{DateTime.Now:O}] {ex}\n\n", Encoding.UTF8);
        }
        catch
        {
            // 真的没辙了就算了 —— 绝不能让游戏因为我们而崩溃。
        }
    }
}
