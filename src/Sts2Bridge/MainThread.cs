using System;
using System.Collections.Concurrent;
using System.Threading;
using System.Threading.Tasks;
using Godot;

namespace Sts2Bridge
{
    /// <summary>
    /// 主线程调度器。
    ///
    /// 【为什么必须有它】
    /// Godot 的 API（以及游戏中绝大多数状态）只能在主线程访问，而 HTTP 请求
    /// 由 HttpListener 在后台线程回调。若直接在后台线程读游戏对象，轻则读到
    /// 撕裂的中间状态，重则直接使游戏崩溃。
    /// 所有对游戏的读写都必须经由本类投递到主线程执行。
    ///
    /// 【接入方式：Godot 信号，而非 Harmony】
    /// 调用栈证明 Entry.Initialize 运行在
    /// NGame..cctor -> NGame._EnterTree -> Godot.Node.InvokeGodotClassMethod 之下，
    /// 即本身就在 Godot 主线程上。因此直接挂 SceneTree.ProcessFrame 信号即可
    /// 接入帧循环，无需引入 Harmony 去 patch 某个逐帧方法。
    /// </summary>
    internal static class MainThread
    {
        private static readonly ConcurrentQueue<Action> Queue = new ConcurrentQueue<Action>();
        private static int _mainThreadId = -1;

        public static bool IsAttached { get; private set; }
        public static bool IsCurrent => System.Environment.CurrentManagedThreadId == _mainThreadId;
        public static long FrameCount { get; private set; }

        /// <summary>必须在主线程调用。</summary>
        public static bool Attach()
        {
            try
            {
                _mainThreadId = System.Environment.CurrentManagedThreadId;

                if (Engine.GetMainLoop() is not SceneTree tree)
                {
                    Log.Write("[MainThread] Engine.GetMainLoop() 尚不可用");
                    return false;
                }

                tree.ProcessFrame += Pump;
                IsAttached = true;
                Log.Write($"[MainThread] 已接入帧循环 (线程 {_mainThreadId})");
                return true;
            }
            catch (Exception ex)
            {
                Log.Error("MainThread.Attach", ex);
                return false;
            }
        }

        private static void Pump()
        {
            FrameCount++;

            // 每帧限量执行，避免积压任务在单帧内全部跑完而造成掉帧
            int budget = 32;
            while (budget-- > 0 && Queue.TryDequeue(out var action))
            {
                try { action(); }
                catch (Exception ex) { Log.Error("主线程任务", ex); }
            }
        }

        /// <summary>把 <paramref name="fn"/> 投递到主线程执行并等待其结果。</summary>
        public static Task<T> Run<T>(Func<T> fn)
        {
            // 已在主线程时直接执行：避免自我死锁（等待一个只有自己才能排空的队列）
            if (IsCurrent)
            {
                try { return Task.FromResult(fn()); }
                catch (Exception ex) { return Task.FromException<T>(ex); }
            }

            if (!IsAttached)
                return Task.FromException<T>(new InvalidOperationException("主线程调度器尚未接入帧循环"));

            var tcs = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);
            Queue.Enqueue(() =>
            {
                try { tcs.TrySetResult(fn()); }
                catch (Exception ex) { tcs.TrySetException(ex); }
            });
            return tcs.Task;
        }

        /// <summary>带超时的同步等待，供 HTTP 处理线程使用。</summary>
        public static T RunSync<T>(Func<T> fn, int timeoutMs = 5000)
        {
            var task = Run(fn);
            if (!task.Wait(timeoutMs))
                throw new TimeoutException($"主线程任务超时 ({timeoutMs} ms) —— 游戏可能已卡死或未在运行");
            return task.Result;
        }
    }
}
