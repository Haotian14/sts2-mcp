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
        private const string CombatRoom       = "NCombatRoom";
        private const string MerchantRoom     = "NMerchantRoom";
        private const string MerchantSlot     = "NMerchantSlot";
        private const string RestSiteRoom     = "NRestSiteRoom";
        private const string RestSiteButton   = "NRestSiteButton";
        private const string TreasureRoom     = "NTreasureRoom";
        private const string RelicHolder      = "NTreasureRoomRelicHolder";

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
        private static void Collect(object? node, string typeName, List<object?> found,
                                    bool includeSubclasses, int depth = 0)
        {
            if (node == null || depth > 12) return;      // 深度上限：防御畸形/循环的场景树

            bool hit = includeSubclasses ? IsA(node, typeName) : GamePaths.Id(node) == typeName;
            if (hit) found.Add(node);

            foreach (var child in Children(node))
                Collect(child, typeName, found, includeSubclasses, depth + 1);
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

        /// <summary>
        /// 按类型短名找出所有后代节点。
        ///
        /// <paramref name="includeSubclasses"/> 为 true 时改按**基类**匹配。
        /// 商店踩过这个坑：`NMerchantSlot` 是抽象基类，场景里的实际节点是
        /// `NMerchantCard` / `NMerchantRelic` / `NMerchantPotion` /
        /// `NMerchantCardRemoval`，按运行时类型短名精确比对**永远匹配 0 个** ——
        /// 商店因此从实现之日起就一直报空，直到 2026-08-01 头一回真站进商店才发现。
        /// </summary>
        private static List<object?> FindAll(object? root, string typeName, bool includeSubclasses = false)
        {
            var found = new List<object?>();
            Collect(root, typeName, found, includeSubclasses);
            return found;
        }

        /// <summary>
        /// 当前「可交互上下文」：优先取可见的覆盖界面，没有则取当前房间节点。
        ///
        /// 休息点、宝箱这些不是覆盖界面，而是**房间节点**
        /// （`/root/Game/RootSceneContainer/Run/RoomContainer/<房间>`），
        /// 只看覆盖层栈会完全看不见它们。对外统一成一个上下文，
        /// 模型不必区分「这是界面还是房间」。
        /// </summary>
        public static object? Context()
        {
            // 地图可走 = 游戏在等你选路，此时任何残留界面都不该再抢镜。
            //
            // 按下「继续」回到地图后，奖励界面**仍留在覆盖层栈上且仍算可见**
            // （实测 ScreenCount=1、IsVisibleInTree 为 true，只有「继续」按钮
            // 变灰了）。仅凭可见性挡不住它，模型会看到一份「既能走地图、又有
            // 三个奖励可领」的自相矛盾的状态。
            // 这两件事在语义上互斥，用互斥关系判定比猜界面的可见性可靠。
            if (MapNav.CanMove) return null;

            var top = Top();
            if (top != null) return top;

            var room = ActiveRoom();
            if (room != null) return room;

            // 没有房间说明还没进局（主菜单、角色选择）。把主菜单也当作上下文，
            // 「死了之后怎么开新局」才有路可走 —— 否则整条链路到此为止。
            return NodeAt(SceneRoot(), "Game", "RootSceneContainer", "MainMenu");
        }

        /// <summary>
        /// 场景树转储 —— 开发期诊断用（`GET /tree`）。
        ///
        /// 界面工作的全部难点都在「那个节点到底叫什么、在哪一层」，而这一点
        /// 无法从反编译代码可靠推断（节点名来自 .tscn 场景文件，不在程序集里）。
        /// 有了它就不必靠猜。
        /// </summary>
        public static string DumpTree(string path, int depth)
        {
            var node = string.IsNullOrWhiteSpace(path)
                ? SceneRoot()
                : NodeAt(SceneRoot(), path.Split('/', StringSplitOptions.RemoveEmptyEntries));

            var w = new JsonWriter();
            w.BeginObject();
            w.Prop("path", path);
            if (node == null)
            {
                w.Prop("found", false);
                w.EndObject();
                return w.ToString();
            }
            w.Prop("found", true);
            w.Prop("type", GamePaths.Id(node));
            WriteNode(w, node, depth);
            w.EndObject();
            return w.ToString();
        }

        private static void WriteNode(JsonWriter w, object? node, int depth)
        {
            w.BeginArray("children");
            if (depth > 0)
            {
                foreach (var child in Children(node))
                {
                    w.BeginObject();
                    w.Prop("name", GamePaths.Text(child, "Name"));
                    w.Prop("type", GamePaths.Id(child));
                    bool visible = false;
                    try { visible = GamePaths.Call(child, "IsVisibleInTree") is bool v && v; } catch { }
                    w.Prop("visible", visible);
                    WriteNode(w, child, depth - 1);
                    w.EndObject();
                }
            }
            w.EndArray();
        }

        /// <summary>房间容器下当前可见的那个房间节点。</summary>
        internal static object? ActiveRoom()
        {
            var container = NodeAt(SceneRoot(), "Game", "RootSceneContainer", "Run", "RoomContainer");
            if (container == null) return null;
            foreach (var room in Children(container))
            {
                try
                {
                    if (GamePaths.Call(room, "IsVisibleInTree") is bool v && v) return room;
                }
                catch { /* 个别节点读不到可见性，跳过 */ }
            }
            return null;
        }

        /// <summary>
        /// 写入 /state 的 screen 段：当前上下文的类型，以及它上面可点的选项。
        ///
        /// 两类界面共用「选项 + 下标」这一个形状，模型只需记住一个动作
        /// （<c>pick</c>）而不是每种界面一个。不认识的界面只报类型名 ——
        /// 让模型知道「卡在一个我处理不了的界面上」，好过什么都不说。
        /// </summary>
        public static void Describe(JsonWriter w)
        {
            var top = Context();
            if (top == null) return;

            var type = GamePaths.Id(top);
            w.BeginObject("screen");
            w.Prop("type", type);

            w.BeginArray("options");
            foreach (var opt in OptionsOf(top, type))
            {
                w.BeginObject();
                w.Prop("i", (int?)OptionIndex(top, type, opt.Node));
                w.Prop("id", opt.Id);
                if (opt.Available.HasValue) w.Prop("available", opt.Available);
                if (opt.Cost.HasValue) w.Prop("cost", opt.Cost);

                // 待选物的名字与效果 —— 只有卡牌/遗物/药水这类有模型的选项才有。
                // 不发进 /glossary 的理由见 GlossaryExporter.TitleOf 上方注释。
                if (opt.Model != null)
                {
                    w.Prop("title", GlossaryExporter.TitleOf(opt.Model));
                    w.Prop("text",  GlossaryExporter.TextOf(opt.Model));
                }
                w.EndObject();
            }
            w.EndArray();

            // 处理完后要按「继续」才会回到地图。按钮存在但未启用时不算 ——
            // 宝箱没开、休息点没选之前它是灰的。
            var proceed = ProceedButtonOf(top);
            w.Prop("can_proceed", proceed != null && (GamePaths.Bool(proceed, "IsEnabled") ?? true));
            w.EndObject();
        }

        /// <summary>
        /// 界面上的一个选项。
        ///
        /// <see cref="Model"/> 是这个选项**对应的东西本身**（卡牌/遗物/药水模型），
        /// 只有待选物才有。有它才能就地渲染出效果文本 —— 光给标识
        /// （`Anger` / `Unrelenting`）等于让模型闭着眼睛选，见任务 6.4c。
        /// </summary>
        private sealed class Option
        {
            public object? Node;
            public string? Id;
            public bool? Available;
            public object? Model;
            public int? Cost;      // 商店价格，其余界面为 null
        }

        /// <summary>
        /// 栈顶界面上可点的选项。顺序即下标，由场景树顺序保证稳定。
        /// </summary>
        private static List<Option> OptionsOf(object? top, string? type)
        {
            var result = new List<Option>();
            switch (type)
            {
                case RewardsScreen:
                    // 奖励按钮：GoldReward / PotionReward / RelicReward / CardReward。
                    // 这一层是**类别**不是具体物品（点开 CardReward 才有三选一），
                    // 故不带模型。
                    foreach (var b in FindAll(top, RewardButton))
                        result.Add(new Option {
                            Node = b,
                            Id = GamePaths.Id(GamePaths.Get(b, "Reward")),
                            Available = GamePaths.Bool(b, "IsEnabled"),
                        });
                    break;

                case CardRewardScreen:
                    // 卡牌三选一：持卡节点，标识取卡牌模型类型短名
                    foreach (var h in FindAll(top, GridCardHolder))
                    {
                        var card = GamePaths.Get(h, "CardModel");
                        result.Add(new Option { Node = h, Id = GamePaths.Id(card), Model = card });
                    }
                    break;

                case RestSiteRoom:
                    // 休息点：烤火回血 / 打铁升级 / …，可用性挂在 Option 上而非按钮上
                    foreach (var b in FindAll(top, RestSiteButton))
                    {
                        var option = GamePaths.Get(b, "Option");
                        result.Add(new Option {
                            Node = b,
                            Id = GamePaths.Text(option, "OptionId") ?? GamePaths.Id(option),
                            Available = GamePaths.Bool(option, "IsEnabled"),
                        });
                    }
                    break;

                case MerchantRoom:
                    // 商店：每个槽位挂一个 MerchantEntry，价格与「钱够不够」都在它上面。
                    // 缺货的槽位不列出 —— 卖掉之后槽位还在，但已无内容。
                    // NMerchantSlot 是抽象基类，必须按基类匹配（见 FindAll）。
                    foreach (var slot in FindAll(top, MerchantSlot, includeSubclasses: true))
                    {
                        var entry = GamePaths.Get(slot, "Entry");
                        if (entry == null) continue;
                        if (!(GamePaths.Bool(entry, "IsStocked") ?? false)) continue;

                        var goods = MerchantGoods(entry);
                        result.Add(new Option {
                            Node = slot,
                            // 除卡服务没有模型，退回 entry 的类型名
                            Id = GamePaths.Id(goods) ?? GamePaths.Id(entry),
                            Available = GamePaths.Bool(entry, "EnoughGold"),
                            Model = goods,
                            Cost = GamePaths.Int(entry, "Cost"),
                        });
                    }
                    break;

                case TreasureRoom:
                    // 宝箱分两步：先开箱，箱开了才有遗物可拿
                    if (!(GamePaths.Bool(top, "_hasChestBeenOpened") ?? false))
                    {
                        var chest = GamePaths.Get(top, "_chestButton");
                        if (chest != null)
                            result.Add(new Option {
                                Node = chest, Id = "Chest", Available = GamePaths.Bool(chest, "IsEnabled"),
                            });
                    }
                    else
                    {
                        foreach (var h in FindAll(top, RelicHolder))
                        {
                            bool visible = false;
                            try { visible = GamePaths.Call(h, "IsVisibleInTree") is bool v && v; } catch { }
                            if (!visible) continue;

                            var relic = GamePaths.Get(GamePaths.Get(h, "Relic"), "Model");
                            result.Add(new Option {
                                Node = h,
                                Id = GamePaths.Id(relic) ?? "Relic",
                                Available = GamePaths.Bool(h, "IsEnabled"),
                                Model = relic,
                            });
                        }
                    }
                    break;

                case CombatRoom:
                    // 战斗房不给选项：出牌走 play_card，房间里那些按钮
                    // （生物的 Hitbox、结束回合按钮…）落进兜底只会变成噪音。
                    // 实测战斗刚结束时曾报出 `[0] 0`、`[2] Hitbox` 这种条目。
                    break;

                default:
                    // 兜底：认不出的界面，就把所有可点、可见、启用的按钮按**节点名**
                    // 列出来。节点名本身是有语义（Continue / SingleplayerButton /
                    // ConfirmButton），模型看得懂。
                    //
                    // 这条兜底的价值在 2026-08-01 那次死亡上体现得很清楚：当时
                    // NGameOverScreen 报了 0 个选项、can_proceed 也是 false，
                    // 整个链路彻底卡死 —— 认不出的界面不该等于死路。
                    //
                    // Boss 遗物三选一预期也落在这里。真站到那个界面前，
                    // 若它同样只报节点名而没有遗物模型，就得像商店那样单开一个 case。
                    foreach (var b in FindClickable(top))
                        result.Add(new Option {
                            Node = b,
                            Id = LabelOf(b) ?? GamePaths.Text(b, "Name"),
                            Available = GamePaths.Bool(b, "IsEnabled"),
                        });
                    break;
            }
            return result;
        }

        /// <summary>
        /// 上下文里所有可见且启用的可点控件（<c>NClickableControl</c> 的子类）。
        /// 按场景树顺序，故下标稳定。
        /// </summary>
        private static List<object?> FindClickable(object? root)
        {
            var found = new List<object?>();
            CollectClickable(root, found);
            return found;
        }

        private static void CollectClickable(object? node, List<object?> found, int depth = 0)
        {
            if (node == null || depth > 12 || found.Count >= 24) return;

            if (IsA(node, "NClickableControl"))
            {
                bool ok = false;
                try
                {
                    ok = GamePaths.Call(node, "IsVisibleInTree") is bool v && v
                         && (GamePaths.Bool(node, "IsEnabled") ?? false);
                }
                catch { }
                if (ok) found.Add(node);
            }

            foreach (var child in Children(node))
                CollectClickable(child, found, depth + 1);
        }

        /// <summary>
        /// 按钮上的可读文本 —— 从它的后代里找第一个非空的 <c>Text</c>。
        ///
        /// 节点名不总是有语义：事件选项的按钮实测叫 `@Control@1132` 这种
        /// Godot 自动生成的名字，对模型毫无信息量。而事件恰恰是最依赖文本的
        /// 场景 —— 「献祭 5 点最大生命换一件遗物」和「离开」只能靠读文本区分。
        ///
        /// 剥掉 Godot 的 BBCode 着色标记，并截断到 80 字符 —— 事件描述可以很长，
        /// 而我们只要选项本身。
        /// </summary>
        private static string? LabelOf(object? node, int depth = 0)
        {
            if (node == null || depth > 4) return null;

            if (GamePaths.TryGet(node, "Text", out var text) && text is string s)
            {
                s = System.Text.RegularExpressions.Regex.Replace(s, @"\[/?[a-zA-Z][^\]]*\]", "").Trim();
                if (s.Length > 0) return s.Length > 80 ? s.Substring(0, 80) + "…" : s;
            }

            foreach (var child in Children(node))
            {
                var found = LabelOf(child, depth + 1);
                if (found != null) return found;
            }
            return null;
        }

        /// <summary>
        /// 把一个不打算 await 的 Task 交给游戏的 <c>TaskHelper.RunSafely</c> ——
        /// 直接丢弃 Task 会让里面的异常无人观察、悄无声息地消失，
        /// 而这类动作失败恰恰是最需要看见的。取不到该助手时退回丢弃。
        /// </summary>
        private static void RunSafely(object? task)
        {
            if (task == null) return;
            try { GamePaths.CallStatic("MegaCrit.Sts2.Core.Helpers.TaskHelper", "RunSafely", task); }
            catch { /* 助手不在就算了，动作本身已经发出去了 */ }
        }

        /// <summary>
        /// 槽位上卖的那件东西本身（卡牌/遗物/药水模型）。除卡服务没有模型，返回 null。
        ///
        /// **不能用 <see cref="LabelOf"/> 顶替** —— 槽位上唯一的文本是价格标签，
        /// 读出来是「54」这样的数字，模型据此根本不知道自己在买什么。
        ///
        /// <code>
        /// MerchantCardEntry    .CreationResult.Card : CardModel   ← 遗物可能改过它，取 Card 而非 originalCard
        /// MerchantRelicEntry   .Model               : RelicModel
        /// MerchantPotionEntry  .Model               : PotionModel
        /// MerchantCardRemovalEntry                    除卡服务，没有模型
        /// </code>
        /// </summary>
        private static object? MerchantGoods(object? entry)
        {
            if (GamePaths.TryGet(entry, "CreationResult", out var created) && created != null)
                return GamePaths.Get(created, "Card");

            return GamePaths.TryGet(entry, "Model", out var model) ? model : null;
        }

        private static bool IsA(object? node, string baseTypeName) => GamePaths.IsA(node, baseTypeName);

        private static int OptionIndex(object? top, string? type, object? node)
        {
            var all = OptionsOf(top, type);
            for (int i = 0; i < all.Count; i++)
                if (ReferenceEquals(all[i].Node, node)) return i;
            return -1;
        }

        /// <summary>点击栈顶界面上的第 index 个选项。须在主线程调用。</summary>
        public static string? Pick(int index)
        {
            var top = Context();
            if (top == null) return "当前没有可交互的界面或房间";

            var type = GamePaths.Id(top);
            var options = OptionsOf(top, type);
            if (options.Count == 0)
                return $"{type} 上没有可点的选项（桥接层尚未支持这个界面）";
            if (index < 0 || index >= options.Count)
                return $"选项下标 {index} 越界（共 {options.Count} 个）";

            var opt = options[index];
            var node = opt.Node;
            if (opt.Available == false)
                return $"第 {index} 个选项（{opt.Id}）当前不可点（可能已领过，钱不够，或药水栏已满）";

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

                case MerchantRoom:
                    // 【不能 ForceClick】商店槽位的 MouseReleased 处理器有
                    // `if (_isHovered && !_ignoreMouseRelease && ev is InputEventMouseButton)`
                    // 三重前置 —— 我们从没悬停过，合成的释放事件被直接丢弃。
                    // 实测：点了没反应，金币不变、界面不变、也不报错。
                    //
                    // 走它自己的选中入口：`OnSelected()` 正是那个处理器校验通过后
                    // 要调的东西（private async Task，内部 await OnTryPurchase）。
                    // 与 CardRewardScreen 直接调 SelectCard 是同一个路数。
                    //
                    // 不 await：游戏自己也是 `TaskHelper.RunSafely(OnSelected())`
                    // 这样发射后不管的，异常交给它记日志；动作是否生效由上层的
                    // 「等局面稳定」判定。
                    RunSafely(GamePaths.Call(node, "OnSelected"));
                    return null;

                default:
                    // 休息点按钮、宝箱、遗物架，以及兜底分支列出的按钮，
                    // 都是 NClickableControl，一律 ForceClick
                    GamePaths.Call(node, "ForceClick");
                    return null;
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
            var top = Context();
            if (top == null) return "当前没有可交互的界面或房间";

            var button = ProceedButtonOf(top);
            if (button == null) return $"{GamePaths.Id(top)} 上没有「继续」按钮";
            if (!(GamePaths.Bool(button, "IsEnabled") ?? true))
                return "「继续」按钮当前不可用（还有事没做完）";

            GamePaths.Call(button, "ForceClick");
            return null;
        }

        /// <summary>
        /// 上下文里的「继续」按钮。
        /// 房间实现了 IRoomWithProceedButton，直接有 ProceedButton 属性；
        /// 覆盖界面没有，只能在节点树里找。
        /// </summary>
        private static object? ProceedButtonOf(object? context)
        {
            if (GamePaths.TryGet(context, "ProceedButton", out var direct) && direct != null)
                return direct;
            var found = FindAll(context, ProceedButton);
            return found.Count > 0 ? found[0] : null;
        }
    }
}
