using System;
using System.IO;
using System.Text;

namespace Sts2Bridge
{
    /// <summary>
    /// 路径解析。
    ///
    /// 桥接层被加载的是 %TEMP% 下的临时副本（见 profiler 的 CopyBridgeToTemp），
    /// 因此**无法**由自身程序集位置反推仓库根目录。仓库路径由启动脚本
    /// launch-steam.cmd 经环境变量 STS2MCP_REPO 传入 —— 它本来就要计算这个
    /// 路径来定位 profiler dll，顺带传递零成本。
    ///
    /// 回退值仅为兜底：早期版本把「仓库位于桌面」写死在代码里，仓库一旦挪走
    /// 日志就会写到不存在的地方，且毫无提示。
    /// </summary>
    internal static class Paths
    {
        public static string RepoRoot { get; }
        public static string LogDir => System.IO.Path.Combine(RepoRoot, "logs");

        static Paths()
        {
            var env = Environment.GetEnvironmentVariable("STS2MCP_REPO");
            if (!string.IsNullOrWhiteSpace(env))
            {
                try
                {
                    var full = System.IO.Path.GetFullPath(env);
                    if (Directory.Exists(full)) { RepoRoot = full; return; }
                }
                catch { }
            }

            RepoRoot = System.IO.Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                "sts2-mcp");
        }
    }

    /// <summary>
    /// 日志。桥接层运行在游戏进程内且无法附加调试器，日志是唯一的观测手段，
    /// 因此每次写入都立即 flush —— 游戏崩溃时缓冲区内容不会丢失。
    /// </summary>
    internal static class Log
    {
        private static readonly object Gate = new object();

        internal static readonly string Path = System.IO.Path.Combine(Paths.LogDir, "bridge.log");

        public static void Write(string msg)
        {
            try
            {
                lock (Gate)
                {
                    Directory.CreateDirectory(System.IO.Path.GetDirectoryName(Path)!);
                    File.AppendAllText(Path,
                        $"[{DateTime.Now:HH:mm:ss.fff}] {msg}{Environment.NewLine}",
                        Encoding.UTF8);
                }
            }
            catch { /* 日志失败绝不能影响游戏 */ }
        }

        public static void Error(string what, Exception ex) => Write($"[错误] {what}: {ex}");
    }
}
