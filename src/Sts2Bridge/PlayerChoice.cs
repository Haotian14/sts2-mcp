namespace Sts2Bridge
{
    /// <summary>
    /// 「游戏正在等玩家做选择」的判定。
    ///
    /// 出牌、用药水都可能跑到一半停下来要一次玩家输入 —— 弃哪张牌、检索哪张、
    /// 「三选一」拿哪个。这是一等一的状态事实，状态导出与动作下发都要用：
    ///
    /// - <see cref="StateExporter"/>：`/state` 必须告诉上层「现在轮到你做选择」。
    ///   否则会像 2026-08-01 那次一样，从手牌张数去推断选择是否已完成 ——
    ///   **选择期间手牌张数根本不变**（弃牌要确认后才生效），推出来的结论是错的。
    /// - <see cref="ActionApi"/>：有未决选择时下发的动作会被游戏**取消**，
    ///   必须直接拒绝，不能报成功。
    ///
    /// 【判据是动作自己的状态，不是队列】
    /// 「队列非空 + 执行器不在跑」既可能是刚入队还没轮到，也可能是停在等选择上，
    /// 两者只有 <c>GameAction.State</c> 分得开。
    /// </summary>
    internal static class PlayerChoice
    {
        private const string RunManagerType   = "MegaCrit.Sts2.Core.Runs.RunManager";
        private const string OverlayStackType = "MegaCrit.Sts2.Core.Nodes.Screens.Overlays.NOverlayStack";

        /// <summary>
        /// 是否有未决的玩家选择。须在主线程调用。
        /// </summary>
        /// <param name="screen">
        /// 弹出式界面的类型名，如 NSimpleCardSelectScreen；
        /// **在手牌里选的那类（弃牌、保留）没有覆盖界面，此处为 null**。
        /// </param>
        public static bool IsPending(out string? screen)
        {
            screen = null;

            object? run = GamePaths.GetStatic(RunManagerType, "Instance");
            object? current = GamePaths.Get(GamePaths.Get(run, "ActionExecutor"), "CurrentlyRunningAction");
            if (GamePaths.Text(current, "State") != "GatheringPlayerChoice") return false;

            screen = CurrentScreen();
            return true;
        }

        /// <summary>
        /// 覆盖层栈顶的界面类型名。游戏自带的 AutoSlay 正是按这个类型分派各界面的
        /// 处理器（见 game-model.md），阶段 3.4 会沿用同一套键。
        ///
        /// 栈为空时返回 null —— 实测「求生者」的弃牌是在手牌里选的，
        /// `ScreenCount` 为 0，不存在对应的覆盖界面。
        /// </summary>
        private static string? CurrentScreen()
        {
            try
            {
                object? stack = GamePaths.GetStatic(OverlayStackType, "Instance");
                if (stack == null) return null;
                if ((GamePaths.Int(stack, "ScreenCount") ?? 0) <= 0) return null;
                // Peek 只是读栈顶的托管引用，不触碰 Godot 原生侧
                return GamePaths.Id(GamePaths.Call(stack, "Peek"));
            }
            catch (System.Exception ex)
            {
                Log.Error("读取当前界面", ex);
                return null;
            }
        }
    }
}
