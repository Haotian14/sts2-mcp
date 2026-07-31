using System;
using System.Collections.Generic;

namespace Sts2Bridge
{
    /// <summary>
    /// 界面层：读取当前覆盖界面上的按钮，并按下标点击。
    ///
    /// 【为什么这里必须点 UI，而战斗与地图不必】
    /// 出牌、移动都有纯模型层入口（<c>TryManualPlay</c> / <c>MoveToMapCoordAction</c>），
    /// 领奖没有。奖励按钮的处理是这样的：
    /// <code>
    /// NRewardButton.OnRelease() → GetReward()
    ///     Disable();
    ///     if (await RunManager.Instance.RewardsSetSynchronizer.SelectLocalReward(Reward))
    ///     { …遗物/药水飞入动画…; EmitSignal(RewardClaimed, this); }   ← 界面靠这个移除按钮
    /// </code>
    /// 中间那行确实是模型层的，但只调它会留下一个已领却还在界面上的按钮，
    /// 且「继续」按钮不会解锁。整套语义收口在按钮上，所以点按钮才是完整的。
    ///
    /// 【澄清一个此前被我夸大的风险】
    /// 早期曾把「调用 Godot API」列为高危。实际当初让游戏 10 毫秒崩溃的是
    /// **编译期引用 GodotSharp** —— 它使该程序集在 Default ALC 中被重复加载，
    /// 形成两套类型标识。而反射调用取到的是游戏 ALC 里**已经存在**的那一份实例，
    /// 不存在重复加载。真正的要求只有一条：**必须在主线程**，而这一点
    /// <see cref="MainThread"/> 已经保证。
    ///
    /// 游戏自带的 AutoSlay 也正是这么点的：<c>UiHelper.Click</c> 的全部实现就是
    /// <c>button.ForceClick()</c>。
    /// </summary>
    internal static class Screens
    {
        private const string OverlayStackType = "MegaCrit.Sts2.Core.Nodes.Screens.Overlays.NOverlayStack";
        private const string RewardsScreen    = "NRewardsScreen";
        private const string RewardButton     = "NRewardButton";
        private const string ProceedButton    = "NProceedButton";
        private const string CardRewardScreen = "NCardRewardSelectionScreen";
        private const string GridCardHolder   = "NGridCardHolder";

        /// <summary>
        /// 覆盖层栈顶界面，没有则 null。须在主线程调用。
        ///
        /// 【为什么还要判可见】
        /// 按下「继续」并回到地图之后，奖励界面**仍留在栈上**（实测
        /// `ScreenCount` 依然是 1），只是已经不可见。只看栈会报出一个空选项、
        /// 却又 `can_proceed=true` 的界面，而此时 `map.can_move` 已经是 true ——
        /// 模型会以为还得再按一次继续。以可见性为准。
        /// </summary>
        public static object? Top()
        {
            var stack = GamePaths.GetStatic(OverlayStackType, "Instance");
            if (stack == null) return null;
            if ((GamePaths.Int(stack, "ScreenCount") ?? 0) <= 0) return null;

            var top = GamePaths.Call(stack, "Peek");
            if (top == null) return null;

            try
            {
                if (GamePaths.Call(top, "IsVisibleInTree") is bool visible && !visible) return null;
            }
            catch (Exception ex)
            {
                // 取不到可见性时宁可当作「界面还在」——漏报界面会让模型无从下手，
                // 多报一个至少还能看出栈顶是什么
                Log.Error("读取界面可见性", ex);
            }
            return top;
        }

        /// <summary>
        /// 在节点树里按类型短名递归收集节点，保持场景树顺序 —— 顺序即下标，
        /// 必须稳定。Godot 的子节点是有序的，天然满足。
        /// </summary>
        private static void Collect(object? node, string typeName, List<object?> found, int depth = 0)
        {
            if (node == null || depth > 12) return;      // 深度上限：防御畸形/循环的场景树

            if (GamePaths.Id(node) == typeName) found.Add(node);

            foreach (var child in Children(node))
                Collect(child, typeName, found, depth + 1);
        }

        /// <summary>
        /// 子节点。用 <c>GetChildren</c> 而不是 <c>GetChildCount</c> + <c>GetChild</c>：
        /// 后两者在 Godot 4 里都带可选参数（<c>includeInternal</c>），而
        /// <c>GetChild</c> 还有泛型重载 —— 按「名称 + 参数个数」解析会挑中泛型
        /// 定义并在 Invoke 时炸掉。<c>GetChildren</c> 返回的 Array&lt;Node&gt;
        /// 可直接枚举，一次调用解决。
        /// </summary>
        private static IEnumerable<object?> Children(object? node)
        {
            object? array;
            try { array = GamePaths.Call(node, "GetChildren", false); }
            catch (MissingMethodException) { array = GamePaths.Call(node, "GetChildren"); }
            return GamePaths.Enumerate(array);
        }

        private static List<object?> FindAll(object? root, string typeName)
        {
            var found = new List<object?>();
            Collect(root, typeName, found);
            return found;
        }

        /// <summary>
        /// 写入 /state 的 screen 段：栈顶界面的类型，以及它上面可点的选项。
        ///
        /// 两类界面共用「选项 + 下标」这一个形状，模型只需记住一个动作
        /// （<c>pick</c>）而不是每种界面一个。不认识的界面只报类型名 ——
        /// 让模型知道「卡在一个我处理不了的界面上」，好过什么都不说。
        /// </summary>
        public static void Describe(JsonWriter w)
        {
            var top = Top();
            if (top == null) return;

            var type = GamePaths.Id(top);
            w.BeginObject("screen");
            w.Prop("type", type);

            w.BeginArray("options");
            foreach (var (node, id, available) in OptionsOf(top, type))
            {
                w.BeginObject();
                w.Prop("i", (int?)OptionIndex(top, type, node));
                w.Prop("id", id);
                if (available.HasValue) w.Prop("available", available);
                w.EndObject();
            }
            w.EndArray();

            // 领完（或决定放弃）后要按「继续」才会回到地图
            w.Prop("can_proceed", FindAll(top, ProceedButton).Count > 0);
            w.EndObject();
        }

        /// <summary>
        /// 栈顶界面上可点的选项。返回节点、可读标识、以及是否可点。
        /// 顺序即下标，由场景树顺序保证稳定。
        /// </summary>
        private static List<(object? node, string? id, bool? available)> OptionsOf(object? top, string? type)
        {
            var result = new List<(object?, string?, bool?)>();
            switch (type)
            {
                case RewardsScreen:
                    // 奖励按钮：GoldReward / PotionReward / RelicReward / CardReward
                    foreach (var b in FindAll(top, RewardButton))
                        result.Add((b, GamePaths.Id(GamePaths.Get(b, "Reward")),
                                    GamePaths.Bool(b, "IsEnabled")));
                    break;

                case CardRewardScreen:
                    // 卡牌三选一：持卡节点，标识取卡牌模型类型短名
                    foreach (var h in FindAll(top, GridCardHolder))
                        result.Add((h, GamePaths.Id(GamePaths.Get(h, "CardModel")), null));
                    break;
            }
            return result;
        }

        private static int OptionIndex(object? top, string? type, object? node)
        {
            var all = OptionsOf(top, type);
            for (int i = 0; i < all.Count; i++)
                if (ReferenceEquals(all[i].node, node)) return i;
            return -1;
        }

        /// <summary>点击栈顶界面上的第 index 个选项。须在主线程调用。</summary>
        public static string? Pick(int index)
        {
            var top = Top();
            if (top == null) return "当前没有打开的界面";

            var type = GamePaths.Id(top);
            var options = OptionsOf(top, type);
            if (options.Count == 0)
                return $"{type} 上没有可点的选项（桥接层尚未支持这个界面）";
            if (index < 0 || index >= options.Count)
                return $"选项下标 {index} 越界（共 {options.Count} 个）";

            var (node, id, available) = options[index];
            if (available == false)
                return $"第 {index} 个选项（{id}）当前不可点（可能已领过，或药水栏已满）";

            switch (type)
            {
                case RewardsScreen:
                    // 奖励按钮是 NClickableControl，ForceClick 会走完整的
                    // OnRelease → GetReward → SelectLocalReward → 发信号让界面移除按钮
                    GamePaths.Call(node, "ForceClick");
                    return null;

                case CardRewardScreen:
                    // 持卡节点继承的是 Godot.Control，没有 ForceClick；
                    // 界面把它的 Pressed 信号接到了自己的私有方法 SelectCard 上，
                    // 直接调该方法与点击等效（它就是把选中下标塞进 TCS）。
                    GamePaths.Call(top, "SelectCard", node);
                    return null;

                default:
                    return $"{type} 不支持点击";
            }
        }

        // ------------------------------------------------------------------
        //  主菜单
        //
        //  重启游戏后会停在主菜单，存档并未载入 —— 自动重启若不连「继续游戏」
        //  一起点掉，就只做了一半。主菜单不在覆盖层栈里，得从场景树根走。
        // ------------------------------------------------------------------

        /// <summary>场景树根节点。</summary>
        private static object? SceneRoot()
        {
            var mainLoop = GamePaths.CallStatic("Godot.Engine", "GetMainLoop");
            return GamePaths.Get(mainLoop, "Root");
        }

        /// <summary>按名字逐层向下找节点。找不到返回 null。</summary>
        private static object? NodeAt(object? node, params string[] names)
        {
            foreach (var name in names)
            {
                object? next = null;
                foreach (var child in Children(node))
                    if (GamePaths.Text(child, "Name") == name) { next = child; break; }
                if (next == null) return null;
                node = next;
            }
            return node;
        }

        /// <summary>主菜单上的「继续游戏」按钮，不可见或不存在时返回 null。</summary>
        private static object? ContinueButton()
        {
            var button = NodeAt(SceneRoot(), "Game", "RootSceneContainer", "MainMenu",
                                "MainMenuTextButtons", "ContinueButton");
            if (button == null) return null;
            // 没有存档时该按钮存在但不可见
            return (GamePaths.Bool(button, "Visible") ?? false) ? button : null;
        }

        /// <summary>是否停在主菜单且可以继续上一局。</summary>
        public static bool CanResumeRun
        {
            get
            {
                try { return ContinueButton() != null; }
                catch (Exception ex) { Log.Error("查找继续按钮", ex); return false; }
            }
        }

        /// <summary>点主菜单的「继续游戏」，载入存档。须在主线程调用。</summary>
        public static string? ResumeRun()
        {
            var button = ContinueButton();
            if (button == null) return "主菜单上没有可用的「继续游戏」按钮（可能已在局中，或没有存档）";
            GamePaths.Call(button, "ForceClick");
            return null;
        }

        /// <summary>按「继续」离开奖励界面。须在主线程调用。</summary>
        public static string? Proceed()
        {
            var top = Top();
            if (top == null) return "当前没有打开的界面";

            var buttons = FindAll(top, ProceedButton);
            if (buttons.Count == 0)
                return $"{GamePaths.Id(top)} 上没有「继续」按钮";

            GamePaths.Call(buttons[0], "ForceClick");
            return null;
        }
    }
}
