using System;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using System.Threading;

namespace Sts2Bridge
{
    /// <summary>
    /// 选牌应答 —— 把「游戏要玩家挑牌」变成一个可由 MCP 回答的问题。
    ///
    /// 【为什么必须做，而且属于战斗内刚需】
    /// 静默猎手的起手牌组里「生存者」「早有准备」都带弃牌，一手五张摸到的
    /// 概率相当高。在此之前，只要打出这类牌，整局就停在那里等人点鼠标 ——
    /// 战斗根本无法自动进行。此前把选牌归到「非战斗场景」是判断失误。
    ///
    /// 【走官方注入点，不点 UI】
    /// 游戏把一切选牌收口到了一个可替换的接口：
    /// <code>
    /// CardSelectCmd.FromHandForDiscard / FromCombatPile / FromSimpleGrid / …
    ///     if (Selector != null) result = await Selector.GetSelectedCards(options, min, max);
    ///     else                  …弹出界面，等玩家点…
    /// </code>
    /// 装上自己的 <c>ICardSelector</c> 即可全部接管。AutoSlay 与游戏内的
    /// 「低语耳环」用的都是这条路。
    ///
    /// 【卡牌奖励不受影响】
    /// 接口的另一个方法 <c>GetSelectedCardReward</c> 只在**没有奖励界面**时才被
    /// 问到（`_currentlyShownScreen != null` 优先），正常游玩时三选一仍走 UI。
    /// 故此处返回默认值即可 —— 注意它是 **struct**，返回 null 会炸。
    ///
    /// 【线程】
    /// <c>GetSelectedCards</c> 由游戏在主线程调用，我们只登记待答请求并立刻
    /// 返回一个未完成的 Task，绝不阻塞。应答时必须**回到主线程**再 SetResult：
    /// TaskCompletionSource 的后续会在完成它的那个线程上就地跑起来，
    /// 而那后续是游戏代码 —— 在 HTTP 线程上跑它等于把整个战斗逻辑搬离主线程。
    /// </summary>
    internal static class CardChoice
    {
        private const string SelectCmdType = "MegaCrit.Sts2.Core.Commands.CardSelectCmd";
        private const string SelectorType  = "MegaCrit.Sts2.Core.TestSupport.ICardSelector";
        private const string CardModelType = "MegaCrit.Sts2.Core.Models.CardModel";

        /// <summary>
        /// 无人应答时的兜底时限。硬挂起比自动替玩家做决定更糟 —— 游戏会永远
        /// 停在那里，且玩家无从得知原因（UI 已被我们绕过，点也没用）。
        /// 超时后取前 min 张并大声记日志。
        /// </summary>
        private const int AnswerTimeoutMs = 180_000;

        private static readonly object Gate = new object();
        private static Pending? _pending;
        private static object? _selector;
        private static Timer? _timeoutTimer;

        /// <summary>一次待答的选牌请求。</summary>
        private sealed class Pending
        {
            public List<object?> Options = new List<object?>();
            public int Min;
            public int Max;
            public object Tcs = null!;     // TaskCompletionSource<IEnumerable<CardModel>>
        }

        public static bool Enabled =>
            Environment.GetEnvironmentVariable("STS2MCP_CHOICE") == "1";

        // ------------------------------------------------------------------
        //  安装
        // ------------------------------------------------------------------

        /// <summary>
        /// 确保选择器已装上。须在主线程调用。
        ///
        /// 每次请求都检查一遍，是因为 <c>CardSelectCmd.Reset()</c> 会在一局
        /// 结束时清空选择器栈（它的注释说是为了清理卡住的异步任务泄漏的
        /// 选择器），我们的也会被一并清掉。
        /// </summary>
        public static void EnsureInstalled()
        {
            if (!Enabled) return;

            try
            {
                lock (Gate)
                {
                    var stack = GamePaths.GetStatic(SelectCmdType, "_selectorStack") as IEnumerable;
                    if (stack == null) return;

                    int count = 0;
                    foreach (var item in stack)
                    {
                        if (ReferenceEquals(item, _selector)) return;   // 已装
                        count++;
                    }

                    // 栈非空但里面不是我们的 —— 游戏正临时压了自己的选择器
                    // （「低语耳环」自动出牌时会这么做）。此时不该插手，
                    // UseSelector 也会直接抛异常。
                    if (count > 0) return;

                    _selector ??= CreateSelector();
                    GamePaths.CallStatic(SelectCmdType, "UseSelector", _selector);
                    Log.Write("[选牌] 已装上选择器，选牌将由 MCP 应答");
                }
            }
            catch (Exception ex)
            {
                Log.Error("安装选牌选择器", ex);
            }
        }

        /// <summary>
        /// 运行时生成 ICardSelector 的实现。
        ///
        /// 桥接层不能编译期引用游戏程序集（会导致其在 Default ALC 中被重复
        /// 加载，游戏必崩），所以没法用 `class X : ICardSelector`。
        /// BCL 的 DispatchProxy 正是为此存在：它按接口在运行时生成代理类型，
        /// 所有调用汇入 <see cref="SelectorProxy.Invoke"/>。
        /// </summary>
        private static object CreateSelector()
        {
            var iface = GamePaths.RequireType(SelectorType);
            var proxy = typeof(SelectorProxy);

            // .NET 9 起有非泛型重载 Create(Type, Type)，正合我们的场景 ——
            // 接口类型只有运行时才知道。注意不能用 GetMethod("Create", …) 直接取：
            // 泛型与非泛型两个重载并存，会抛 AmbiguousMatchException（实测踩过）。
            foreach (var m in typeof(System.Reflection.DispatchProxy)
                         .GetMethods(BindingFlags.Public | BindingFlags.Static))
            {
                if (m.Name != "Create" || m.IsGenericMethodDefinition) continue;
                var ps = m.GetParameters();
                if (ps.Length == 2 && ps[0].ParameterType == typeof(Type))
                    return m.Invoke(null, new object[] { iface, proxy })!;
            }

            // 兜底：老运行时只有 Create<T, TProxy>()
            foreach (var m in typeof(System.Reflection.DispatchProxy)
                         .GetMethods(BindingFlags.Public | BindingFlags.Static))
            {
                if (m.Name != "Create" || !m.IsGenericMethodDefinition) continue;
                if (m.GetGenericArguments().Length == 2 && m.GetParameters().Length == 0)
                    return m.MakeGenericMethod(iface, proxy).Invoke(null, null)!;
            }

            throw new MissingMethodException("DispatchProxy 上找不到可用的 Create 重载");
        }

        // ------------------------------------------------------------------
        //  登记与应答
        // ------------------------------------------------------------------

        /// <summary>由游戏在主线程调用。登记请求，立刻返回未完成的 Task。</summary>
        internal static object BeginSelection(object? options, int min, int max)
        {
            var cardType = GamePaths.RequireType(CardModelType);
            var tcsType = typeof(System.Threading.Tasks.TaskCompletionSource<>)
                .MakeGenericType(typeof(IEnumerable<>).MakeGenericType(cardType));
            var tcs = Activator.CreateInstance(tcsType)!;

            var pending = new Pending { Min = min, Max = max, Tcs = tcs };
            foreach (var card in GamePaths.Enumerate(options)) pending.Options.Add(card);

            lock (Gate)
            {
                if (_pending != null)
                    Log.Write("[选牌] 上一个选牌请求尚未应答就来了新的 —— 覆盖旧的");
                _pending = pending;

                _timeoutTimer?.Dispose();
                _timeoutTimer = new Timer(_ => OnTimeout(pending), null, AnswerTimeoutMs, Timeout.Infinite);
            }

            Log.Write($"[选牌] 待答：{pending.Options.Count} 选 {min}~{max}");
            return GamePaths.Get(tcs, "Task")!;
        }

        private static void OnTimeout(Pending pending)
        {
            lock (Gate) { if (!ReferenceEquals(_pending, pending)) return; }

            Log.Write($"[选牌] {AnswerTimeoutMs / 1000} 秒无人应答，自动取前 {pending.Min} 张放行。"
                      + "选牌 UI 已被选择器绕过，不放行游戏会永远停住");

            var indices = new List<int>();
            for (int i = 0; i < pending.Min && i < pending.Options.Count; i++) indices.Add(i);
            try { MainThread.RunSync(() => Resolve(indices)); }
            catch (Exception ex) { Log.Error("选牌超时兜底", ex); }
        }

        public static bool IsPending
        {
            get { lock (Gate) return _pending != null; }
        }

        /// <summary>把待答请求写进 JSON。无待答请求时什么都不写。</summary>
        public static void Describe(JsonWriter w)
        {
            Pending? pending;
            lock (Gate) pending = _pending;
            if (pending == null) return;

            w.BeginObject("choice");
            w.Prop("kind", "cards");
            w.Prop("min", (int?)pending.Min);
            w.Prop("max", (int?)pending.Max);
            w.BeginArray("options");
            for (int i = 0; i < pending.Options.Count; i++)
            {
                var card = pending.Options[i];
                w.BeginObject();
                w.Prop("i", (int?)i);
                w.Prop("id", GamePaths.Id(card));
                var cost = GamePaths.Int(GamePaths.Get(card, "EnergyCost"), "Canonical");
                w.Prop("cost", cost.HasValue && cost.Value < 0 ? null : cost);
                w.Prop("type", GamePaths.Text(card, "Type"));
                w.EndObject();
            }
            w.EndArray();
            w.EndObject();
        }

        /// <summary>
        /// 应答。须在主线程调用 —— TaskCompletionSource 的后续（游戏代码）
        /// 会在完成它的线程上就地执行。
        /// </summary>
        /// <returns>出错原因，成功为 null。</returns>
        public static string? Resolve(List<int> indices)
        {
            Pending? pending;
            lock (Gate) pending = _pending;
            if (pending == null) return "当前没有待答的选牌请求";

            if (indices.Count < pending.Min || indices.Count > pending.Max)
                return $"要选 {pending.Min}~{pending.Max} 张，收到 {indices.Count} 张";

            var seen = new HashSet<int>();
            foreach (var i in indices)
            {
                if (i < 0 || i >= pending.Options.Count)
                    return $"下标 {i} 越界（共 {pending.Options.Count} 个选项）";
                if (!seen.Add(i)) return $"下标 {i} 重复";
            }

            var cardType = GamePaths.RequireType(CardModelType);
            var list = (IList)Activator.CreateInstance(typeof(List<>).MakeGenericType(cardType))!;
            foreach (var i in indices) list.Add(pending.Options[i]);

            lock (Gate)
            {
                _pending = null;
                _timeoutTimer?.Dispose();
                _timeoutTimer = null;
            }

            // SetResult 会就地跑起游戏的后续逻辑，故必须在主线程 —— 由调用方保证
            GamePaths.Call(pending.Tcs, "SetResult", list);
            Log.Write($"[选牌] 已应答：{indices.Count} 张");
            return null;
        }
    }

    /// <summary>
    /// <c>ICardSelector</c> 的运行时实现载体。DispatchProxy 会**继承**本类型来
    /// 生成代理，因此有两条硬性要求 —— 违反了只在运行时才报错：
    ///
    /// - **不能是 sealed**（实测报 "The base type … cannot be sealed"）；
    /// - **必须是 public 顶层类型**：生成的代理位于另一个动态程序集中，
    ///   继承 internal 或嵌套类型会撞可访问性。
    ///
    /// 接口方法只有两个，直接按方法名分派即可。
    /// </summary>
    public class SelectorProxy : System.Reflection.DispatchProxy
    {
        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            switch (method?.Name)
            {
                case "GetSelectedCards":
                    return CardChoice.BeginSelection(args![0], Convert.ToInt32(args[1]), Convert.ToInt32(args[2]));

                case "GetSelectedCardReward":
                    // 正常游玩时走不到这里（有奖励界面就轮不到选择器）。
                    // 返回类型是 struct，必须给默认实例而非 null。
                    Log.Write("[选牌] GetSelectedCardReward 被调用 —— 未预期，返回默认值");
                    return Activator.CreateInstance(method!.ReturnType);

                default:
                    // 接口将来若加了方法，返回类型安全的默认值，绝不抛 ——
                    // 这里是游戏的调用栈，异常会波及战斗流程。
                    Log.Write($"[选牌] 未知的接口方法 {method?.Name}，返回默认值");
                    return method != null && method.ReturnType != typeof(void) && method.ReturnType.IsValueType
                        ? Activator.CreateInstance(method.ReturnType)
                        : null;
            }
        }
    }
}
