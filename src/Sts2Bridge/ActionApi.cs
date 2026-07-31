using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;

namespace Sts2Bridge
{
    /// <summary>
    /// 动作下发 —— 让 Claude 真正操纵游戏。
    ///
    /// 【走游戏自己的手动出牌入口，不自造动作】
    /// 实测（ILSpy 反编译 sts2.dll v0.107.1）玩家点击一张牌时，UI 最终调用的是：
    /// <code>
    /// CardModel.TryManualPlay(Creature target)
    ///   → CanPlayTargeting(target)                    合法性判定
    ///   → EnqueueManualPlay(target)
    ///       → OnEnqueuePlayVfx(target)                出牌特效
    ///       → RunManager.Instance.ActionQueueSynchronizer
    ///             .RequestEnqueue(new PlayCardAction(this, target))
    /// </code>
    /// 我们调的就是这一个方法。此前 game-model.md 里写的是自行构造
    /// <c>PlayCardAction(player, card, target, ctx)</c> 再往 ActionQueueSet 里
    /// <c>EnqueueWithoutSynchronizing</c> —— 那是照着文档注释推断的，三处都不对：
    /// 构造函数其实是 <c>(CardModel, Creature)</c> 两参、入队要经
    /// ActionQueueSynchronizer 而非直接进 ActionQueueSet、且会漏掉出牌特效。
    /// 用游戏自己的入口就没有「哪一步漏了」的问题。
    ///
    /// 药水同理走 <c>PotionModel.EnqueueManualUse(target)</c>，
    /// 结束回合走 <c>PlayerCmd.EndTurn(player, canBackOut:false, null)</c> ——
    /// 后者是游戏内「结束回合」按钮与 AutoSlay 冒烟测试共用的那一个。
    ///
    /// 【不要用 CardCmd.AutoPlay】
    /// 游戏自带的 AutoSlay 冒烟测试用的是它，但那是**免费打出**（不消耗能量）
    /// 的自动打出效果通道，服务于劫掠、复制药水一类。用它模拟玩家出牌是作弊。
    ///
    /// 【必须已接入帧循环】
    /// 从后台线程往动作队列入队会损坏队列结构。所有动作一律先检查
    /// <see cref="MainThread.IsAttached"/>，未接入即拒绝执行 —— 只读查询可以
    /// 走降级路径，动作不行。
    ///
    /// 【错误是正常结果，不是故障】
    /// 能量不够、目标非法、不在出牌阶段 —— 这些都返回 HTTP 200 且
    /// <c>ok:false</c> + 结构化的 error/reason，让上层能分辨「这步不能走」
    /// 与「桥接层坏了」。只有请求本身畸形才抛异常（由 HttpApi 转 400）。
    /// </summary>
    internal static class ActionApi
    {
        private const string CombatManagerType = "MegaCrit.Sts2.Core.Combat.CombatManager";
        private const string RunManagerType    = "MegaCrit.Sts2.Core.Runs.RunManager";
        private const string PlayerCmdType     = "MegaCrit.Sts2.Core.Commands.PlayerCmd";

        /// <summary>
        /// 等待局面稳定的默认上限。结束回合要等完整个敌方回合（含动画），
        /// 20 秒是给最慢的多怪回合留的余量。
        /// </summary>
        private const int DefaultTimeoutMs = 20000;

        /// <summary>轮询间隔。每次轮询都要排到主线程执行，60fps 下一帧约 16 ms。</summary>
        private const int PollIntervalMs = 30;

        /// <summary>
        /// 入队后至少观察这么久才允许判定为「已稳定」。
        ///
        /// 入队与执行之间隔着 ActionQueueSynchronizer 的消息投递，动作未必在
        /// 下一帧就被执行器接手。若不设这个下限，可能在队列还没开始动的瞬间
        /// 就把「空队列」误读成「已执行完」，从而返回一份尚未生效的状态。
        /// 一旦真正观察到队列忙过，这个下限即失效（见 sawBusy）。
        /// </summary>
        private const int MinObserveMs = 250;

        /// <summary>
        /// 判定「在等玩家做选择」前，该状态需持续这么久。
        ///
        /// 游戏内部也会短暂进入 GatheringPlayerChoice 再自行走完（由 hook 自动
        /// 应答的那类）。只看一帧会把这种一闪而过的中间态误报成「等你选牌」。
        /// </summary>
        private const int ChoiceConfirmMs = 500;

        // ------------------------------------------------------------------
        //  入口
        // ------------------------------------------------------------------

        /// <summary>
        /// 执行一个动作。verb 取自 URL 路径（/action/&lt;verb&gt;），参数取自 query。
        /// </summary>
        public static string Perform(string verb, Dictionary<string, string> q)
        {
            bool withState = !(q.TryGetValue("state", out var s) && (s == "0" || s == "false"));
            int timeoutMs = q.TryGetValue("timeout", out var t) && int.TryParse(t, out var tv) && tv > 0
                ? Math.Min(tv, 120000)
                : DefaultTimeoutMs;

            // 未接入帧循环时连读状态都不敢附带 —— 此刻任何游戏访问都在 HTTP 线程上
            if (!MainThread.IsAttached)
                return Fail(verb, "not_attached",
                    "未接入 Godot 帧循环，拒绝下发动作：从后台线程入队会损坏游戏的动作队列。"
                    + "请确认启动器设置了 STS2MCP_ATTACH_FRAME=1，并查看 /health 的 attached 字段", null, false);

            var plan = MainThread.RunSync(() => Begin(verb, q));

            if (!plan.Ok)
            {
                Log.Write($"[动作] {verb} 被拒绝: {plan.Error}"
                          + (plan.Reason != null ? $" ({plan.Reason})" : ""));
                return Fail(verb, plan.Error!, plan.Detail, plan.Reason, withState, plan.Screen);
            }

            Log.Write($"[动作] {verb} {plan.Summary}");

            var outcome = WaitUntilSettled(plan, timeoutMs);

            var w = new JsonWriter();
            w.BeginObject();
            w.Prop("ok", true);
            w.Prop("action", verb);
            foreach (var (key, value) in plan.Labels) w.Prop(key, value);
            // settled=false 表示局面仍未稳定：动作已下发，但随附的 state
            // 可能是中间态。上层应重新拉一次 /state，而不是照它做下一步决策。
            w.Prop("settled", outcome.Settled);
            if (outcome.AwaitingChoice)
            {
                w.Prop("awaiting_choice", true);
                w.Prop("screen", outcome.Screen);
            }
            w.Prop("waited_ms", outcome.WaitedMs);
            if (withState) w.Raw("state", SafeState());
            w.EndObject();
            return w.ToString();
        }

        // ------------------------------------------------------------------
        //  第一步：主线程上校验并下发
        // ------------------------------------------------------------------

        /// <summary>
        /// 一次动作下发的结果：要么带着「等什么」的信息成功入队，
        /// 要么带着结构化原因失败。全部字段在主线程上填好，供 HTTP 线程使用。
        /// </summary>
        private sealed class Plan
        {
            public bool Ok;
            public string? Error;
            public string? Detail;
            public string? Reason;
            /// <summary>驳回原因涉及某个界面时给出其类型名。</summary>
            public string? Screen;

            public string Verb = "";
            /// <summary>回给调用方的动作描述字段，如 card=StrikeSilent、target=CorpseSlug。</summary>
            public readonly List<(string, string?)> Labels = new List<(string, string?)>();
            /// <summary>结束回合专用：下发前的回合数，用来识别「新回合已经开始」。</summary>
            public int? BaselineTurn;
            /// <summary>移动专用：目标坐标，用来识别「真的走到了」。</summary>
            public int TargetRow = -1;
            public int TargetCol = -1;

            public string Summary
            {
                get
                {
                    var parts = new List<string>();
                    foreach (var (k, v) in Labels) if (v != null) parts.Add($"{k}={v}");
                    return string.Join(" ", parts);
                }
            }

            public static Plan Reject(string error, string? detail = null,
                                      string? reason = null, string? screen = null) =>
                new Plan { Ok = false, Error = error, Detail = detail, Reason = reason, Screen = screen };
        }

        private static Plan Begin(string verb, Dictionary<string, string> q)
        {
            // 每次都确认选择器还在：CardSelectCmd.Reset() 会在一局结束时
            // 清空选择器栈，我们的也会被一并清掉。
            CardChoice.EnsureInstalled();

            // 有未决的玩家选择时一律拒绝下发。
            //
            // 【为什么必须拒绝，而不是排队等】
            // 实测（2026-08-01）在「求生者」的弃牌选择未决时下发「中和」，
            // 桥接层报了 ok:true，牌却原封不动留在手里、敌人毫发无损 ——
            // 入队的出牌被游戏**取消**了。PlayCardAction 为此专门重写了
            // CancelAction，注释写得很直白：
            //   "some external action (like showing the hand selection screen)
            //    needs to cancel queued card plays"
            // 报成功而实际没发生，是所有错误里最坏的一种：上层会照着一个
            // 从未生效的动作继续往下推。
            // choose 本身就是用来应答选择的，自然不受此限
            if (verb != "choose")
            {
                if (CardChoice.IsPending)
                    return Plan.Reject("awaiting_choice",
                        "游戏正在等待选牌。请先用 choose 应答（选项见 /state 的 choice 字段）");

                if (PlayerChoice.IsPending(out var pendingScreen))
                    return Plan.Reject("awaiting_choice",
                        "游戏正在等待玩家做出选择（弃牌 / 选牌 / 三选一），"
                        + "此时下发的动作会被游戏取消。须先应答该选择"
                        + (pendingScreen != null ? $"（界面 {pendingScreen}）" : "（在手牌中选择，无弹出界面）"),
                        screen: pendingScreen);
            }

            switch (verb)
            {
                case "play_card":  return BeginPlayCard(q);
                case "end_turn":   return BeginEndTurn();
                case "use_potion": return BeginUsePotion(q);
                case "choose":     return BeginChoose(q);
                case "move":       return BeginMove(q);
                default:
                    throw new ArgumentException(
                        $"未知动作: {verb}（可用: play_card / end_turn / use_potion）");
            }
        }

        private static Plan BeginPlayCard(Dictionary<string, string> q)
        {
            int cardIndex = RequireInt(q, "card");

            var ctx = Context.Capture();
            var gate = ctx.RequireCombatReady();
            if (gate != null) return gate;

            // 手牌挂在 PlayerCombatState 上（血量才在 Creature 上，见 game-model.md）
            var hand = GamePaths.Get(GamePaths.Get(ctx.PlayerCombat, "Hand"), "Cards");
            var card = GamePaths.At(hand, cardIndex);
            if (card == null)
                return Plan.Reject("bad_index",
                    $"手牌下标 {cardIndex} 越界（当前手牌 {GamePaths.Count(hand) ?? 0} 张）");

            // 目标下标与 /state 的 enemies[].i 一一对应，用的必须是同一个集合
            object? target = null;
            if (q.TryGetValue("target", out var rawTarget) && rawTarget.Length > 0)
            {
                int targetIndex = ParseInt(rawTarget, "target");
                var enemies = GamePaths.Get(ctx.CombatState, "Enemies");
                target = GamePaths.At(enemies, targetIndex);
                if (target == null)
                    return Plan.Reject("bad_index",
                        $"目标下标 {targetIndex} 越界（当前敌人 {GamePaths.Count(enemies) ?? 0} 个）");
            }

            // CanPlay 才是游戏用来置灰卡牌的判定（IsPlayable 不是，见 game-model.md）。
            // 先自己判一次，是为了把 TryManualPlay 的一个 false 拆成有原因的错误。
            var args = new object?[2];
            if (GamePaths.Call(card, "CanPlay", args) is bool can && !can)
                return Plan.Reject("unplayable",
                    $"{GamePaths.Id(card)} 现在打不出", args[0]?.ToString());

            if (GamePaths.Call(card, "IsValidTarget", target) is bool valid && !valid)
            {
                var tt = GamePaths.Text(card, "TargetType");
                return Plan.Reject("bad_target",
                    target == null
                        ? $"{GamePaths.Id(card)} 的 TargetType 为 {tt}，必须指定 target"
                        : $"{GamePaths.Id(card)} 的 TargetType 为 {tt}，不接受这个目标"
                          + "（Self/AllEnemies 一类不可传 target；目标须存活且阵营正确）");
            }

            // 至此前置条件都已自检通过，TryManualPlay 仍返回 false 属于我们没料到的情形
            if (GamePaths.Call(card, "TryManualPlay", target) is bool played && !played)
                return Plan.Reject("rejected",
                    $"游戏拒绝打出 {GamePaths.Id(card)}，且预检未能给出原因");

            var plan = new Plan { Ok = true, Verb = "play_card" };
            plan.Labels.Add(("card", GamePaths.Id(card)));
            plan.Labels.Add(("target", CreatureLabel(target)));
            return plan;
        }

        /// <summary>
        /// 应答一次待答的选牌。<c>cards</c> 是逗号分隔的下标，取自
        /// <c>/state</c> 的 <c>choice.options[].i</c>；允许为空（min 为 0 时即跳过）。
        /// </summary>
        private static Plan BeginChoose(Dictionary<string, string> q)
        {
            if (!CardChoice.IsPending)
                return Plan.Reject("no_choice", "当前没有待答的选牌请求");

            var indices = new List<int>();
            if (q.TryGetValue("cards", out var raw) && raw.Length > 0)
                foreach (var part in raw.Split(','))
                {
                    var trimmed = part.Trim();
                    if (trimmed.Length > 0) indices.Add(ParseInt(trimmed, "cards"));
                }

            // Resolve 会就地跑起游戏的后续逻辑 —— 此处已在主线程上（Begin 由
            // MainThread.RunSync 调度），正是它要求的执行位置。
            var error = CardChoice.Resolve(indices);
            if (error != null) return Plan.Reject("bad_choice", error);

            var plan = new Plan { Ok = true, Verb = "choose" };
            plan.Labels.Add(("chose", string.Join(",", indices)));
            return plan;
        }

        /// <summary>走到地图上的第 <c>node</c> 个可走节点（下标取自 /state 的 map.options[].i）。</summary>
        private static Plan BeginMove(Dictionary<string, string> q)
        {
            int index = RequireInt(q, "node");

            var error = MapNav.Move(index, out int row, out int col);
            if (error != null) return Plan.Reject("cannot_move", error);

            var plan = new Plan { Ok = true, Verb = "move" };
            plan.TargetRow = row;
            plan.TargetCol = col;
            plan.Labels.Add(("node", $"({row},{col})"));
            return plan;
        }

        private static Plan BeginEndTurn()
        {
            var ctx = Context.Capture();
            var gate = ctx.RequireCombatReady();
            if (gate != null) return gate;

            // canBackOut 是多人模式里「反悔」用的，单机固定 false；
            // 第三参 actionDuringEnemyTurn 是测试钩子，传 null。
            // 三个参数都要显式给出 —— 反射调用不会代填默认值。
            GamePaths.CallStatic(PlayerCmdType, "EndTurn", ctx.Player, false, null);

            var plan = new Plan { Ok = true, Verb = "end_turn" };
            plan.BaselineTurn = GamePaths.Int(ctx.PlayerCombat, "TurnNumber");
            plan.Labels.Add(("turn", plan.BaselineTurn?.ToString()));
            return plan;
        }

        private static Plan BeginUsePotion(Dictionary<string, string> q)
        {
            int slot = RequireInt(q, "slot");

            var ctx = Context.Capture();
            if (ctx.Player == null)
                return Plan.Reject("no_run", "当前没有进行中的爬塔");

            // 药水在战斗外也能喝（UsePotionAction 会带上 IsInProgress 标记），
            // 故这里只在战斗中才要求出牌阶段。
            if (ctx.InCombat)
            {
                var gate = ctx.RequireCombatReady();
                if (gate != null) return gate;
            }

            var slots = GamePaths.Get(ctx.Player, "PotionSlots");
            var potion = GamePaths.At(slots, slot);
            if (potion == null)
                return Plan.Reject("empty_slot",
                    $"药水槽 {slot} 为空或越界（共 {GamePaths.Count(slots) ?? 0} 个槽）");

            if (GamePaths.TryGet(potion, "IsQueued", out var queued) && queued is bool qb && qb)
                return Plan.Reject("already_queued", $"{GamePaths.Id(potion)} 已在队列中，勿重复下发");

            object? target = null;
            if (q.TryGetValue("target", out var rawTarget) && rawTarget.Length > 0)
            {
                if (!ctx.InCombat)
                    return Plan.Reject("bad_target", "战斗外不能指定目标");
                int targetIndex = ParseInt(rawTarget, "target");
                var enemies = GamePaths.Get(ctx.CombatState, "Enemies");
                target = GamePaths.At(enemies, targetIndex);
                if (target == null)
                    return Plan.Reject("bad_index",
                        $"目标下标 {targetIndex} 越界（当前敌人 {GamePaths.Count(enemies) ?? 0} 个）");
            }
            else if (ctx.InCombat)
            {
                // 复刻 EnqueueManualUse 内部的兜底：不给目标时，若自身是合法目标
                // 就指向自己。药水的 TargetType.Self **要**传目标（与卡牌相反，
                // 游戏源码里专门注释了这处不对称），若不先补上，下面的 IsValidTarget
                // 会把「喝一瓶加血药」误判为非法。
                var self = GamePaths.Get(ctx.Player, "Creature");
                if (GamePaths.Call(potion, "IsValidTarget", self) is bool selfOk && selfOk)
                    target = self;
            }

            if (GamePaths.Call(potion, "IsValidTarget", target) is bool valid && !valid)
                return Plan.Reject("bad_target",
                    $"{GamePaths.Id(potion)} 的 TargetType 为 {GamePaths.Text(potion, "TargetType")}，"
                    + (target == null ? "必须指定 target" : "不接受这个目标"));

            GamePaths.Call(potion, "EnqueueManualUse", target);

            var plan = new Plan { Ok = true, Verb = "use_potion" };
            plan.Labels.Add(("potion", GamePaths.Id(potion)));
            plan.Labels.Add(("target", CreatureLabel(target)));
            return plan;
        }

        // ------------------------------------------------------------------
        //  第二步：等到局面稳定（3.5 就绪判据）
        //
        //  动作是异步执行的：入队后要经执行器取出、跑完效果、可能连锁触发别的
        //  动作（弃牌时自动打出的 Sly 牌、遗物响应……）。若立刻返回状态，读到的
        //  是中间态 —— 能量已扣、伤害未结算，模型照着它做决策必然出错。
        // ------------------------------------------------------------------

        private readonly struct Outcome
        {
            public readonly bool Settled;
            public readonly long WaitedMs;
            public readonly bool AwaitingChoice;
            public readonly string? Screen;

            public Outcome(bool settled, long waitedMs, bool awaitingChoice = false, string? screen = null)
            {
                Settled = settled; WaitedMs = waitedMs;
                AwaitingChoice = awaitingChoice; Screen = screen;
            }
        }

        private static Outcome WaitUntilSettled(Plan plan, int timeoutMs)
        {
            var sw = Stopwatch.StartNew();
            bool sawBusy = false;
            long choiceSince = -1;

            while (sw.ElapsedMilliseconds < timeoutMs)
            {
                Thread.Sleep(PollIntervalMs);

                Probe p;
                try { p = MainThread.RunSync(Probe.Take, 5000); }
                catch (Exception ex)
                {
                    // 主线程卡住时不该把整个请求变成 500：如实返回未稳定即可
                    Log.Error("等待动作稳定", ex);
                    return new Outcome(false, sw.ElapsedMilliseconds);
                }

                // 动作跑到一半停下来等玩家做选择（弃牌、检索、「X 选 1」……）。
                // 这不是超时，是一个需要上层继续应答的中间态 —— 必须立刻回报，
                // 否则就是白等满整个 timeout 再报一个语焉不详的 settled:false。
                // 实测「求生者」（获得格挡并弃一张牌）就会走到这里。
                if (p.AwaitingChoice)
                {
                    if (choiceSince < 0) choiceSince = sw.ElapsedMilliseconds;
                    if (sw.ElapsedMilliseconds - choiceSince >= ChoiceConfirmMs)
                    {
                        Log.Write($"[动作] {plan.Verb} 停在玩家选择上: {p.Screen ?? "未知界面"}");
                        return new Outcome(false, sw.ElapsedMilliseconds, true, p.Screen);
                    }
                    continue;
                }
                choiceSince = -1;

                if (p.Busy) { sawBusy = true; continue; }

                // 移动要过三道关，少一道都会返回中间态：
                //
                //   1. 走到了目标坐标 —— 但坐标**先于**房间就位；
                //   2. 房间已经建好 —— 实测只判坐标时返回过 room=null、
                //      in_combat=false 的快照，紧接着的一次调用却报「当前房间
                //      CombatRoom」，即房间是在我们返回之后才装配起来的；
                //   3. 若进的是战斗房，还要等战斗开打并进入出牌阶段，
                //      否则模型会拿到一份牌还没发完的手牌。
                if (plan.Verb == "move")
                {
                    if (p.MapRow != plan.TargetRow || p.MapCol != plan.TargetCol) continue;
                    if (p.Room == null) continue;
                    if (p.Room == "CombatRoom" && (!p.InCombat || p.Phase != "Play")) continue;
                    return new Outcome(true, sw.ElapsedMilliseconds);
                }

                // 战斗结束（打赢、被打死、逃脱）——不必再等回合阶段
                if (!p.InCombat) return new Outcome(true, sw.ElapsedMilliseconds);

                // 仍在 AutoPrePlay / AutoPostPlay / 敌方回合
                if (p.Phase != "Play") continue;

                // 结束回合的判据是「新回合真的开始了」。仅看队列空会在敌方回合
                // 尚未入队的那一瞬间提前返回。
                if (plan.Verb == "end_turn" && plan.BaselineTurn.HasValue &&
                    (p.Turn ?? int.MinValue) <= plan.BaselineTurn.Value) continue;

                if (!sawBusy && sw.ElapsedMilliseconds < MinObserveMs) continue;

                return new Outcome(true, sw.ElapsedMilliseconds);
            }

            Log.Write($"[动作] {plan.Verb} 等待稳定超时（{timeoutMs} ms），返回的状态可能是中间态");
            return new Outcome(false, sw.ElapsedMilliseconds);
        }

        /// <summary>某一帧上的「游戏在忙吗」快照。只在主线程采集。</summary>
        private readonly struct Probe
        {
            public readonly bool Busy;
            public readonly bool InCombat;
            public readonly string? Phase;
            public readonly int? Turn;
            /// <summary>当前动作停在 GatheringPlayerChoice 上，等着有人做选择。</summary>
            public readonly bool AwaitingChoice;
            /// <summary>等选择时栈顶界面的类型名，如 NSimpleCardSelectScreen。</summary>
            public readonly string? Screen;
            /// <summary>当前地图坐标，未走第一步时为 -1。</summary>
            public readonly int MapRow;
            public readonly int MapCol;
            /// <summary>当前房间类型名。房间切换途中为 null。</summary>
            public readonly string? Room;

            private Probe(bool busy, bool inCombat, string? phase, int? turn,
                          bool awaitingChoice, string? screen, int mapRow, int mapCol, string? room)
            {
                Busy = busy; InCombat = inCombat; Phase = phase; Turn = turn;
                AwaitingChoice = awaitingChoice; Screen = screen;
                MapRow = mapRow; MapCol = mapCol; Room = room;
            }

            public static Probe Take()
            {
                object? run = GamePaths.GetStatic(RunManagerType, "Instance");
                bool queueBusy = !(GamePaths.Bool(GamePaths.Get(run, "ActionQueueSet"), "IsEmpty") ?? true);
                object? executor = GamePaths.Get(run, "ActionExecutor");
                bool executing = GamePaths.Bool(executor, "IsRunning") ?? false;

                // 两种「在等选择」：游戏自己的选牌界面（动作 State 为
                // GatheringPlayerChoice），以及我们装的选择器登记的待答请求。
                // 后者不走 SignalPlayerChoiceBegun，动作 State 不会变，
                // 只看前者会让等待循环一直空转到超时。
                bool awaiting = PlayerChoice.IsPending(out var screen) || CardChoice.IsPending;

                object? cm = GamePaths.GetStatic(CombatManagerType, "Instance");
                bool inCombat = GamePaths.Bool(cm, "IsInProgress") ?? false;

                // 移动的完成判据。CurrentMapCoord 是可空结构体，未走第一步时为 null。
                // 房间类型要一并取：坐标先于房间就位，只看坐标会读到中间态。
                var runState = GamePaths.Get(run, "State");
                var mapCoord = GamePaths.Get(runState, "CurrentMapCoord");
                int mapRow = GamePaths.Int(mapCoord, "row") ?? -1;
                int mapCol = GamePaths.Int(mapCoord, "col") ?? -1;
                string? room = GamePaths.Id(GamePaths.Get(runState, "CurrentRoom"));

                bool effect = false;
                string? phase = null;
                int? turn = null;

                if (inCombat)
                {
                    var player = LocalPlayer();
                    // 效果可能嵌套触发（弃牌触发的自动打出等），队列空了也未必真结束
                    effect = GamePaths.Call(cm, "IsExecutingCardOrPotionEffect", player) is bool e && e;
                    var pcs = GamePaths.Get(player, "PlayerCombatState");
                    phase = GamePaths.Text(pcs, "Phase");
                    turn = GamePaths.Int(pcs, "TurnNumber");
                }

                return new Probe(queueBusy || executing || effect, inCombat, phase, turn,
                                 awaiting, screen, mapRow, mapCol, room);
            }
        }

        // ------------------------------------------------------------------
        //  游戏对象定位
        // ------------------------------------------------------------------

        /// <summary>下发动作所需的一组游戏对象，一次取齐避免各处重复判空。</summary>
        private readonly struct Context
        {
            public readonly object? Combat;
            public readonly object? CombatState;
            public readonly object? Player;
            public readonly object? PlayerCombat;
            public readonly bool InCombat;

            private Context(object? combat, object? combatState, object? player, object? pcs, bool inCombat)
            {
                Combat = combat; CombatState = combatState; Player = player;
                PlayerCombat = pcs; InCombat = inCombat;
            }

            public static Context Capture()
            {
                object? cm = GamePaths.GetStatic(CombatManagerType, "Instance");
                bool inCombat = GamePaths.Bool(cm, "IsInProgress") ?? false;
                object? state = inCombat ? GamePaths.Get(cm, "_state") : null;
                object? player = LocalPlayer();
                return new Context(cm, state, player,
                    inCombat ? GamePaths.Get(player, "PlayerCombatState") : null, inCombat);
            }

            /// <summary>战斗内动作的共同前置。通过返回 null，否则返回该驳回哪一条。</summary>
            public Plan? RequireCombatReady()
            {
                if (!InCombat) return Plan.Reject("not_in_combat", "当前不在战斗中");
                if (Player == null) return Plan.Reject("no_player", "取不到玩家对象");

                // 结算动画、选牌界面等期间游戏自己也会禁掉玩家操作
                if (GamePaths.Bool(Combat, "PlayerActionsDisabled") ?? false)
                    return Plan.Reject("actions_disabled", "游戏当前禁用了玩家操作（结算或界面弹出中）");

                var phase = GamePaths.Text(PlayerCombat, "Phase");
                if (phase != "Play")
                    return Plan.Reject("not_ready",
                        $"当前回合阶段为 {phase ?? "未知"}，仅 Play 阶段可下发动作");

                return null;
            }
        }

        /// <summary>
        /// 生物的可读标识。<c>Creature</c> 的运行时类型名恒为 "Creature"，
        /// 直接用它回报目标毫无信息量，也对不上 /state 里的 <c>enemies[].id</c>
        /// —— 那边取的是 <c>Monster</c> 模型的类型名。此处保持一致。
        /// </summary>
        private static string? CreatureLabel(object? creature)
        {
            if (creature == null) return null;
            var monster = GamePaths.Get(creature, "Monster");
            return monster != null ? GamePaths.Id(monster) : "player";
        }

        /// <summary>
        /// 本地玩家。单机下就是玩家列表的第一个 —— 必须与 StateExporter 取到
        /// 同一个对象，否则 /state 里的手牌下标与这里的对不上。
        /// </summary>
        private static object? LocalPlayer()
        {
            object? cm = GamePaths.GetStatic(CombatManagerType, "Instance");
            if (GamePaths.Bool(cm, "IsInProgress") ?? false)
            {
                var p = GamePaths.First(GamePaths.Get(GamePaths.Get(cm, "_state"), "Players"));
                if (p != null) return p;
            }
            object? run = GamePaths.GetStatic(RunManagerType, "Instance");
            return GamePaths.First(GamePaths.Get(GamePaths.Get(run, "State"), "Players"));
        }

        // ------------------------------------------------------------------
        //  辅助
        // ------------------------------------------------------------------

        private static int RequireInt(Dictionary<string, string> q, string key) =>
            q.TryGetValue(key, out var raw)
                ? ParseInt(raw, key)
                : throw new ArgumentException($"缺少参数 {key}");

        private static int ParseInt(string raw, string key) =>
            int.TryParse(raw, out var v)
                ? v
                : throw new ArgumentException($"参数 {key} 不是整数: {raw}");

        private static string Fail(string verb, string error, string? detail,
                                   string? reason = null, bool withState = true,
                                   string? screen = null)
        {
            var w = new JsonWriter();
            w.BeginObject();
            w.Prop("ok", false);
            w.Prop("action", verb);
            w.Prop("error", error);
            if (reason != null) w.Prop("reason", reason);
            if (screen != null) w.Prop("screen", screen);
            if (detail != null) w.Prop("detail", detail);
            // 附上当前状态：动作被拒时模型多半是照着过期状态在决策，
            // 直接给它一份新的，省掉一次往返。
            if (withState) w.Raw("state", SafeState());
            w.EndObject();
            return w.ToString();
        }

        /// <summary>取状态。状态导出本身出问题时不应把动作结果一起带崩。</summary>
        private static string SafeState()
        {
            try { return MainThread.RunSync(StateExporter.Export); }
            catch (Exception ex)
            {
                Log.Error("动作后取状态", ex);
                return "{\"ok\":false,\"error\":" + Reflect.Str(ex.GetBaseException().Message) + "}";
            }
        }
    }
}
