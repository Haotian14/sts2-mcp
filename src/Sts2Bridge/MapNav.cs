using System;
using System.Collections.Generic;

namespace Sts2Bridge
{
    /// <summary>
    /// 地图：可走节点的导出与移动。
    ///
    /// 【不必碰 UI】
    /// 玩家点击地图节点后，游戏走的是一条纯模型层的路：
    /// <code>
    /// // MapSelectionSynchronizer.MoveToMapCoord()
    /// var action = new MoveToMapCoordAction(LocalContext.GetMe(runState), coord);
    /// actionQueueSynchronizer.RequestEnqueue(action);
    /// </code>
    /// 与出牌完全同形 —— 构造 GameAction，走我们已经在用的同一个入队通道。
    /// 动作内部再去驱动地图动画与 <c>RunManager.EnterMapCoord</c>。
    /// 桥接层因此**不需要调用任何 Godot 原生 API**，绕开了「第一次碰 GodotSharp」
    /// 的风险（当初正是编译期引用 GodotSharp 让游戏 10 毫秒硬崩溃）。
    ///
    /// 【下标必须稳定】
    /// <c>MapPoint.Children</c> 是 <c>HashSet</c>，枚举顺序不保证稳定。模型读到
    /// 一份选项、再按下标移动，两次枚举顺序不同就会走错节点。故一律按
    /// (row, col) 排序，且导出与执行**共用** <see cref="Options"/> 这一个入口。
    /// </summary>
    internal static class MapNav
    {
        private const string RunManagerType = "MegaCrit.Sts2.Core.Runs.RunManager";
        private const string MoveActionType = "MegaCrit.Sts2.Core.GameActions.MoveToMapCoordAction";
        private const string MapScreenType  = "MegaCrit.Sts2.Core.Nodes.Screens.Map.NMapScreen";

        private static object? RunState =>
            GamePaths.Get(GamePaths.GetStatic(RunManagerType, "Instance"), "State");

        /// <summary>
        /// 现在能不能走。
        ///
        /// 【判据不是房间类型】
        /// 初版用的是 <c>CurrentRoom is MapRoom</c>，实测恒为 false ——
        /// **地图在 StS2 里是一个界面而非房间**。打完一个房间后地图界面浮出来，
        /// 而 <c>CurrentRoom</c> 仍停在刚打完的那个房间上（实测停在 EventRoom
        /// 且 <c>IsPreFinished = true</c>）。<c>MapRoom</c> 只在特定流程里用到。
        ///
        /// <c>NMapScreen</c> 的 <c>IsOpen</c> 与 <c>IsTravelEnabled</c> 都是纯托管
        /// 的自动属性（读它们不触碰 Godot 原生侧），后者正是游戏自己用来控制
        /// 「此刻可否点节点」的开关 —— 比我们自己推断房间状态可靠得多。
        /// </summary>
        public static bool CanMove
        {
            get
            {
                var screen = GamePaths.GetStatic(MapScreenType, "Instance");
                if (screen == null) return false;
                return (GamePaths.Bool(screen, "IsOpen") ?? false)
                    && (GamePaths.Bool(screen, "IsTravelEnabled") ?? false);
            }
        }

        /// <summary>
        /// 下一步可走的节点，按 (row, col) 排序。
        ///
        /// 本章尚未走第一步时（<c>VisitedMapCoords</c> 为空），可选的是第 0 行的
        /// 全部节点；否则是当前节点的子节点。这与游戏自带的 AutoSlay 判据一致。
        /// </summary>
        public static List<object?> Options()
        {
            var state = RunState;
            var result = new List<object?>();
            if (state == null) return result;

            int visited = GamePaths.Count(GamePaths.Get(state, "VisitedMapCoords")) ?? 0;
            if (visited == 0)
            {
                // Grid 是全图节点。首步没有「当前节点」可依，只能按行号筛。
                foreach (var p in GamePaths.Enumerate(GamePaths.Get(GamePaths.Get(state, "Map"), "Grid")))
                    if (Row(p) == 0) result.Add(p);
            }
            else
            {
                var current = GamePaths.Get(state, "CurrentMapPoint");
                foreach (var p in GamePaths.Enumerate(GamePaths.Get(current, "Children")))
                    result.Add(p);
            }

            result.Sort((a, b) =>
            {
                int byRow = Row(a).CompareTo(Row(b));
                return byRow != 0 ? byRow : Col(a).CompareTo(Col(b));
            });
            return result;
        }

        private static int Row(object? point) => GamePaths.Int(GamePaths.Get(point, "coord"), "row") ?? -1;
        private static int Col(object? point) => GamePaths.Int(GamePaths.Get(point, "coord"), "col") ?? -1;

        /// <summary>写入 /state 的 map 段。须在主线程调用。</summary>
        public static void Describe(JsonWriter w)
        {
            var state = RunState;
            if (state == null) return;

            w.BeginObject("map");

            var coord = GamePaths.Get(state, "CurrentMapCoord");
            if (coord != null)
            {
                w.BeginObject("coord");
                w.Prop("row", GamePaths.Int(coord, "row"));
                w.Prop("col", GamePaths.Int(coord, "col"));
                w.EndObject();
            }
            else
            {
                w.Prop("coord", (string?)null);   // 本章还没走第一步
            }

            // 只有停在地图界面上才能移动；战斗中、商店里给出选项只会误导模型
            bool canMove = CanMove;
            w.Prop("can_move", canMove);

            w.BeginArray("options");
            if (canMove)
            {
                var options = Options();
                for (int i = 0; i < options.Count; i++)
                {
                    w.BeginObject();
                    w.Prop("i", (int?)i);
                    w.Prop("row", (int?)Row(options[i]));
                    w.Prop("col", (int?)Col(options[i]));
                    // Monster / Elite / Shop / RestSite / Treasure / Boss / Ancient / Unknown
                    w.Prop("type", GamePaths.Text(options[i], "PointType"));
                    w.EndObject();
                }
            }
            w.EndArray();

            w.EndObject();
        }

        /// <summary>
        /// 移动到第 index 个可走节点。须在主线程调用（入队要碰动作队列）。
        /// </summary>
        /// <param name="targetRow">成功时回填目标行，供等待逻辑判断是否走到了。</param>
        /// <returns>出错原因，成功为 null。</returns>
        public static string? Move(int index, out int targetRow, out int targetCol)
        {
            targetRow = -1;
            targetCol = -1;

            if (!CanMove)
                return "地图界面未打开或当前不允许移动"
                     + $"（当前房间 {GamePaths.Id(GamePaths.Get(RunState, "CurrentRoom")) ?? "未知"}）";

            var options = Options();
            if (options.Count == 0) return "当前没有可走的节点";
            if (index < 0 || index >= options.Count)
                return $"节点下标 {index} 越界（共 {options.Count} 个可走节点）";

            var point = options[index];
            var coord = GamePaths.Get(point, "coord")
                        ?? throw new MissingMemberException("MapPoint 上没有 coord");
            targetRow = Row(point);
            targetCol = Col(point);

            var player = GamePaths.First(GamePaths.Get(RunState, "Players"));
            if (player == null) return "取不到玩家对象";

            // 与游戏自己的做法逐字一致：构造 MoveToMapCoordAction 后经
            // ActionQueueSynchronizer 入队（不是 ActionQueueSet）。
            var action = Activator.CreateInstance(
                GamePaths.RequireType(MoveActionType), new[] { player, coord })!;

            var sync = GamePaths.Get(GamePaths.GetStatic(RunManagerType, "Instance"), "ActionQueueSynchronizer");
            GamePaths.Call(sync, "RequestEnqueue", action);
            return null;
        }
    }
}
