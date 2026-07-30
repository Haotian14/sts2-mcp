using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;

namespace Sts2Bridge
{
    /// <summary>
    /// 托管桥接层入口 —— 由 Sts2Profiler 注入的 IL 调用。
    ///
    /// 调用点：<c>MegaCrit.Sts2.Core.Nodes.NGame..cctor</c> 的开头。
    /// 注入的 IL 形如：
    /// <code>
    ///   ldstr  "&lt;本 dll 路径&gt;"
    ///   call   Assembly Assembly::LoadFrom(string)   // 载入 Default ALC
    ///   pop
    ///   call   void Sts2Bridge.Entry::Initialize()   // 此时已可解析
    ///   &lt;原 .cctor 的 27 字节 IL&gt;
    /// </code>
    ///
    /// 【必须全程吞异常】
    /// 本方法运行在静态构造函数内部。.cctor 中任何未捕获的异常都会被 CLR
    /// 包装为 TypeInitializationException，并且该类型将被永久标记为不可用 ——
    /// 对 NGame 这个游戏主类而言，等同于游戏必崩。
    /// 因此这里绝不允许异常逃逸，宁可静默失败也不能影响游戏。
    /// </summary>
    public sealed class Entry
    {
        /// <summary>
        /// 注入的 IL 通过 <c>Activator.CreateInstance(Type)</c> 调用本构造函数。
        ///
        /// 为何不让注入的 IL 直接 <c>call Entry::Initialize()</c>：
        /// 方法体是整体 JIT 的，JIT 编译 .cctor 时就要解析该 call 的目标，
        /// 这发生在同一方法体内的 Assembly.LoadFrom 执行**之前**，
        /// 必然抛 FileNotFoundException（Sts2Bridge 不在游戏的探测路径中）。
        /// 因此注入的 IL 只能引用 BCL 类型，对本程序集一律走反射。
        /// </summary>
        public Entry() => Initialize();

        private static readonly string LogPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
            "sts2-mcp", "logs", "bridge.log");

        private static bool _initialized;

        /// <summary>
        /// 注入的 IL 直接调用此方法。签名必须保持 <c>static void ()</c> ——
        /// profiler 侧按 { 0x00, 0x00, 0x01 }（DEFAULT 调用约定 / 0 参数 / void）
        /// 构造 MemberRef 签名，改动签名会导致注入的 IL 失效。
        /// </summary>
        public static void Initialize()
        {
            try
            {
                // .cctor 虽由 CLR 保证只执行一次，但仍加此保护：
                // 将来若把注入点换成 get_Instance 之类的高频方法，这里不必再改。
                if (_initialized) return;
                _initialized = true;

                Directory.CreateDirectory(Path.GetDirectoryName(LogPath)!);

                var sb = new StringBuilder();
                sb.AppendLine();
                sb.AppendLine("================================================================");
                sb.AppendLine($"  托管桥接层已启动  {DateTime.Now:yyyy-MM-dd HH:mm:ss.fff}");
                sb.AppendLine("================================================================");

                var proc = Process.GetCurrentProcess();
                sb.AppendLine($"进程        : {proc.ProcessName} (PID {proc.Id})");
                sb.AppendLine($"CLR         : {Environment.Version}");
                sb.AppendLine($"本程序集    : {typeof(Entry).Assembly.Location}");
                sb.AppendLine($"调用栈深度  : {new StackTrace().FrameCount}");

                // 确认调用确实来自 NGame..cctor —— 这是注入是否命中预期落点的证据
                sb.AppendLine("调用栈:");
                var st = new StackTrace(false);
                for (int i = 0; i < Math.Min(st.FrameCount, 6); i++)
                {
                    var m = st.GetFrame(i)?.GetMethod();
                    if (m != null)
                        sb.AppendLine($"  [{i}] {m.DeclaringType?.FullName}::{m.Name}");
                }

                var loaded = AppDomain.CurrentDomain.GetAssemblies();
                sb.AppendLine($"已加载程序集: {loaded.Length}");

                var sts2 = loaded.FirstOrDefault(a =>
                    string.Equals(a.GetName().Name, "sts2", StringComparison.OrdinalIgnoreCase));

                if (sts2 == null)
                {
                    sb.AppendLine("[!] 未找到 sts2 程序集 —— 这不应该发生");
                }
                else
                {
                    sb.AppendLine($"sts2        : v{sts2.GetName().Version}");
                    sb.AppendLine();
                    sb.AppendLine("关键类型可达性（决定阶段 2 能否落地）:");

                    foreach (var t in new[]
                    {
                        "MegaCrit.Sts2.Core.Nodes.NGame",
                        "MegaCrit.Sts2.Core.Combat.CombatManager",
                        "MegaCrit.Sts2.Core.Runs.RunManager",
                        "MegaCrit.Sts2.Core.Commands.CardCmd",
                        "MegaCrit.Sts2.Core.Commands.PlayerCmd",
                        "MegaCrit.Sts2.Core.Models.CardModel",
                        "MegaCrit.Sts2.Core.Entities.Multiplayer.NetFullCombatState",
                        "MegaCrit.Sts2.Core.Saves.JsonSerializationUtility",
                    })
                    {
                        Type? ty = null;
                        try { ty = sts2.GetType(t, false); } catch { }
                        sb.AppendLine(ty != null
                            ? $"  [✓] {t}"
                            : $"  [✗] {t}");
                    }

                    // Harmony 是游戏自带的直接依赖（见 sts2.deps.json），
                    // 阶段 1.4 起要靠它打补丁，这里先确认能否解析到
                    var harmony = loaded.FirstOrDefault(a =>
                        string.Equals(a.GetName().Name, "0Harmony", StringComparison.OrdinalIgnoreCase));
                    sb.AppendLine();
                    sb.AppendLine(harmony != null
                        ? $"0Harmony    : 已加载 v{harmony.GetName().Version}"
                        : "0Harmony    : 尚未加载（按需加载，属正常）");
                }

                sb.AppendLine("================================================================");
                File.AppendAllText(LogPath, sb.ToString(), Encoding.UTF8);

                ScheduleDeferredInit();
            }
            catch (Exception ex)
            {
                // 连日志都写不出时的兜底。绝不能让异常逃逸到 .cctor 外。
                try
                {
                    File.AppendAllText(
                        Path.Combine(Path.GetTempPath(), "sts2-mcp-entry-error.log"),
                        $"[{DateTime.Now:O}] {ex}\n\n", Encoding.UTF8);
                }
                catch { /* 放弃，但绝不抛出 */ }
            }
        }

        /// <summary>
        /// 安排真正的初始化。
        ///
        /// 【为何必须延迟】
        /// 本方法运行时，NGame 正处在 _EnterTree 执行途中 —— 静态构造函数
        /// 被它触发，场景树尚未构建完毕，此刻 Engine.GetMainLoop() 未必可用，
        /// 更不宜在其中挂信号或启服务。
        /// CallDeferred 会把调用推迟到当前帧的空闲期，届时场景树已就绪，
        /// 且仍在主线程上。
        /// </summary>
        private static void ScheduleDeferredInit()
        {
            try
            {
                Godot.Callable.From(DeferredInit).CallDeferred();
                Log.Write("[Entry] 已通过 CallDeferred 安排延迟初始化");
            }
            catch (Exception ex)
            {
                // CallDeferred 不可用时退化为轮询等待主循环就绪
                Log.Error("CallDeferred 不可用，改用轮询", ex);
                var t = new System.Threading.Thread(PollUntilReady)
                {
                    IsBackground = true,
                    Name = "sts2-mcp-init-poll",
                };
                t.Start();
            }
        }

        private static void DeferredInit()
        {
            try
            {
                Log.Write("[Entry] 延迟初始化开始");

                if (!MainThread.Attach())
                {
                    // 场景树仍未就绪，再推迟一帧
                    Log.Write("[Entry] 主循环尚未就绪，顺延一帧重试");
                    Godot.Callable.From(DeferredInit).CallDeferred();
                    return;
                }

                HttpApi.Start();
                Log.Write("[Entry] 初始化完成 —— 桥接层已就绪");
            }
            catch (Exception ex)
            {
                Log.Error("延迟初始化", ex);
            }
        }

        private static void PollUntilReady()
        {
            for (int i = 0; i < 120; i++)          // 最多等 60 秒
            {
                try
                {
                    if (Godot.Engine.GetMainLoop() != null)
                    {
                        Godot.Callable.From(DeferredInit).CallDeferred();
                        return;
                    }
                }
                catch { /* 主循环尚不可用，继续等 */ }
                System.Threading.Thread.Sleep(500);
            }
            Log.Write("[Entry] 等待主循环超时，桥接层未能启动");
        }
    }
}
