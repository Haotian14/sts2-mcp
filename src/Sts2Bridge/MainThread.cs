using System;
using System.Collections.Concurrent;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;

namespace Sts2Bridge
{
    /// <summary>
    /// 主线程调度器。
    ///
    /// 【为什么需要】
    /// Godot API 与游戏状态只应在主线程访问，而 HTTP 请求由后台线程回调。
    /// 在后台线程直接读写游戏对象，轻则读到撕裂的中间状态，重则崩溃。
    ///
    /// 【为什么全程反射】
    /// 绝不可编译期引用 GodotSharp。游戏用自定义 AssemblyLoadContext 加载
    /// 自身程序集，而桥接层位于 Default ALC；编译期引用会导致 GodotSharp 在
    /// Default ALC 中被重复加载，形成两套类型标识，使游戏崩溃（详见 csproj
    /// 注释与 spec）。反射取到的是游戏 ALC 中已存在的那一份实例。
    ///
    /// 【接入时机：必须在主线程】
    /// 订阅 SceneTree.ProcessFrame 这个动作本身就是一次 Godot 原生调用，
    /// 因而也必须在主线程完成 —— 曾经以为这构成先有鸡还是先有蛋的死结。
    /// 解法是 <see cref="Entry.Initialize"/>：它由 NGame..cctor 触发，调用栈为
    /// NGame._EnterTree -> .cctor，是全流程中唯一已确证位于主线程的时机，
    /// 且此刻场景树必然已存在。实测一次即成功。
    ///
    /// 【降级路径仍然保留】
    /// 接入失败时任务在调用线程直接执行。只读查询尚可接受（最坏读到某一帧的
    /// 中间状态），但**下发动作时绝不可依赖降级路径** —— 从后台线程往
    /// ActionQueueSet 入队会损坏队列结构。动作接口须先检查 IsAttached。
    /// </summary>
    internal static class MainThread
    {
        private static readonly ConcurrentQueue<Action> Queue = new ConcurrentQueue<Action>();
        private static int _mainThreadId = -1;
        private static int _degradedWarned;

        public static bool IsAttached { get; private set; }
        public static bool IsCurrent => System.Environment.CurrentManagedThreadId == _mainThreadId;
        public static long FrameCount { get; private set; }

        /// <summary>记录主线程 id。须在主线程调用。</summary>
        public static void MarkCurrentAsMain()
        {
            _mainThreadId = System.Environment.CurrentManagedThreadId;
        }

        /// <summary>尝试经反射订阅 Godot 的逐帧信号。失败不致命，转入降级模式。</summary>
        public static bool TryAttach()
        {
            try
            {
                var engineType = FindGameType("Godot.Engine");
                if (engineType == null) { Log.Write("[MainThread] 找不到 Godot.Engine"); return false; }

                var mainLoop = engineType
                    .GetMethod("GetMainLoop", BindingFlags.Public | BindingFlags.Static)
                    ?.Invoke(null, null);
                if (mainLoop == null) { Log.Write("[MainThread] GetMainLoop 返回 null（主循环尚未就绪）"); return false; }

                var evt = mainLoop.GetType().GetEvent("ProcessFrame");
                if (evt?.EventHandlerType == null)
                {
                    Log.Write($"[MainThread] {mainLoop.GetType().FullName} 上没有 ProcessFrame 事件");
                    return false;
                }

                var pump = typeof(MainThread).GetMethod(nameof(Pump),
                    BindingFlags.NonPublic | BindingFlags.Static)!;
                var handler = Delegate.CreateDelegate(evt.EventHandlerType, pump);
                evt.AddEventHandler(mainLoop, handler);

                IsAttached = true;
                Log.Write($"[MainThread] 已接入帧循环 ({mainLoop.GetType().FullName})");
                return true;
            }
            catch (Exception ex)
            {
                Log.Error("MainThread.TryAttach", ex);
                return false;
            }
        }

        private static void Pump()
        {
            FrameCount++;
            if (_mainThreadId < 0) _mainThreadId = System.Environment.CurrentManagedThreadId;

            // 每帧限量执行，避免积压任务在单帧内跑完而造成掉帧
            int budget = 32;
            while (budget-- > 0 && Queue.TryDequeue(out var action))
            {
                try { action(); }
                catch (Exception ex) { Log.Error("主线程任务", ex); }
            }
        }

        public static Task<T> Run<T>(Func<T> fn)
        {
            if (IsCurrent || !IsAttached)
            {
                if (!IsAttached && Interlocked.Exchange(ref _degradedWarned, 1) == 0)
                    Log.Write("[MainThread] 降级：未接入帧循环，任务将在调用线程直接执行");

                try { return Task.FromResult(fn()); }
                catch (Exception ex) { return Task.FromException<T>(ex); }
            }

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
                throw new TimeoutException($"主线程任务超时 ({timeoutMs} ms) —— 游戏可能已卡死");
            return task.GetAwaiter().GetResult();
        }

        /// <summary>在所有已加载程序集（含游戏自定义 ALC）中查找类型。</summary>
        internal static Type? FindGameType(string fullName)
        {
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                try
                {
                    var t = asm.GetType(fullName, false);
                    if (t != null) return t;
                }
                catch { }
            }
            return null;
        }
    }
}
