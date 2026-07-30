using System;
using System.IO;
using System.Text;

namespace Sts2Bridge
{
    /// <summary>
    /// 日志。桥接层运行在游戏进程内且无法附加调试器，日志是唯一的观测手段，
    /// 因此每次写入都立即 flush —— 游戏崩溃时缓冲区内容不会丢失。
    /// </summary>
    internal static class Log
    {
        private static readonly object Gate = new object();

        internal static readonly string Path = System.IO.Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
            "sts2-mcp", "logs", "bridge.log");

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
