using System;
using System.Collections.Generic;

namespace Sts2Bridge
{
    /// <summary>
    /// 把游戏运行时状态压成一份紧凑 JSON，供 MCP server 读取。
    ///
    /// 【设计要点：只发动态数据】
    /// 卡牌描述、遗物说明这类**静态卡面文本**一律不进 /state —— 它们每回合
    /// 一字不变，却能轻易占掉整个载荷。此处只发标识与本局会变的数字，
    /// 文本由 /cards 单独提供、由 MCP server 缓存。这是 token 成本的关键。
    ///
    /// 【设计要点：标识用英文类型短名】
    /// StrikeSilent 而非「打击」。稳定、与语言设置无关、可作为字典键，
    /// 也让决策日志能跨局比对。
    ///
    /// 【设计要点：逐字段容错】
    /// 游戏处于抢先体验期，任一成员都可能在下个版本消失。单个字段读失败时
    /// 填 null 并追加一条 warnings，整体仍返回 200 —— 爬到一半因为某个没见过
    /// 的遗物导致接口整个挂掉是不可接受的。
    /// </summary>
    internal static class StateExporter
    {
        private const string CombatManager = "MegaCrit.Sts2.Core.Combat.CombatManager";
        private const string RunManager    = "MegaCrit.Sts2.Core.Runs.RunManager";

        public static string Export()
        {
            var w = new JsonWriter();
            var g = new Guarded();

            object? combat = g.Static(CombatManager, "Instance");
            object? run    = g.Static(RunManager, "Instance");

            bool inCombat = combat != null && (g.Bool(combat, "IsInProgress") ?? false);
            object? combatState = inCombat ? g.Obj(combat, "_state") : null;

            object? runState = g.Obj(run, "State");

            // 战斗内外都从 Player 取遗物/药水/金币 —— 两处拿到的是同一个对象，
            // 这样 /state 在地图与商店界面也能给出有意义的输出。
            object? player = inCombat
                ? FirstOf(g.Obj(combatState, "Players"))
                : FirstOf(g.Obj(runState, "Players"));

            w.BeginObject();
            w.Prop("ok", true);
            w.Prop("in_combat", inCombat);

            // 主线程接入状态。降级模式下本次读取发生在 HTTP 线程，理论上可能
            // 跨帧撕裂 —— 数据对不上时先看这里。
            w.Prop("attached", MainThread.IsAttached);
            w.Prop("frame", MainThread.FrameCount);

            WriteRun(w, g, runState, player);

            if (inCombat)
            {
                object? pcs = g.Obj(player, "PlayerCombatState");
                WriteCombat(w, g, combatState, pcs);
                WritePlayer(w, g, player);
                WriteEnemies(w, g, combatState);
                WriteHand(w, g, pcs);
                WritePiles(w, g, pcs);
            }

            WriteInventory(w, g, player);

            w.BeginArray("warnings");
            foreach (var msg in g.Warnings) w.Value(msg);
            w.EndArray();

            w.EndObject();
            return w.ToString();
        }

        // ------------------------------------------------------------------
        //  爬塔层面
        // ------------------------------------------------------------------
        private static void WriteRun(JsonWriter w, Guarded g, object? runState, object? player)
        {
            w.BeginObject("run");
            // CurrentActIndex 是 0 基的，对外统一按玩家看到的第 1 章计数
            // null + 1 仍为 null，读失败时不会伪造出一个「第 1 章」
            w.Prop("act", g.Int(runState, "CurrentActIndex") + 1);
            w.Prop("floor", g.Int(runState, "ActFloor"));
            w.Prop("total_floor", g.Int(runState, "TotalFloor"));
            w.Prop("ascension", g.Int(runState, "AscensionLevel"));
            w.Prop("room", GamePaths.Id(g.Obj(runState, "CurrentRoom")));
            w.Prop("location", g.Text(runState, "RunLocation"));
            w.Prop("game_over", g.Bool(runState, "IsGameOver") ?? false);
            w.Prop("gold", g.Int(player, "Gold"));
            w.EndObject();
        }

        // ------------------------------------------------------------------
        //  战斗层面
        // ------------------------------------------------------------------
        private static void WriteCombat(JsonWriter w, Guarded g, object? combatState, object? pcs)
        {
            w.BeginObject("combat");
            w.Prop("turn", g.Int(pcs, "TurnNumber"));
            w.Prop("round", g.Int(combatState, "RoundNumber"));
            // Phase 是下发动作的就绪判据：仅 Play 时出牌才安全
            w.Prop("phase", g.Text(pcs, "Phase"));
            w.Prop("side", g.Text(combatState, "CurrentSide"));
            w.Prop("energy", g.Int(pcs, "Energy"));
            w.Prop("max_energy", g.Int(pcs, "MaxEnergy"));
            w.Prop("stars", g.Int(pcs, "Stars"));
            w.Prop("encounter", GamePaths.Id(g.Obj(combatState, "Encounter")));
            w.EndObject();
        }

        private static void WritePlayer(JsonWriter w, Guarded g, object? player)
        {
            // 血量与格挡挂在 Creature 上，手牌与能量挂在 PlayerCombatState 上，
            // 两者不是同一个对象（见 game-model.md）。
            object? creature = g.Obj(player, "Creature");

            w.BeginObject("player");
            w.Prop("character", GamePaths.Id(g.Obj(player, "Character")));
            w.Prop("hp", g.Int(creature, "CurrentHp"));
            w.Prop("max_hp", g.Int(creature, "MaxHp"));
            w.Prop("block", g.Int(creature, "Block"));
            WritePowers(w, g, creature);
            w.EndObject();
        }

        private static void WriteEnemies(JsonWriter w, Guarded g, object? combatState)
        {
            w.BeginArray("enemies");
            int? i = 0;
            foreach (var enemy in GamePaths.Enumerate(g.Obj(combatState, "Enemies")))
            {
                w.BeginObject();
                w.Prop("i", i++);
                object? monster = g.Obj(enemy, "Monster");
                w.Prop("id", GamePaths.Id(monster));
                w.Prop("hp", g.Int(enemy, "CurrentHp"));
                w.Prop("max_hp", g.Int(enemy, "MaxHp"));
                w.Prop("block", g.Int(enemy, "Block"));
                w.Prop("alive", g.Bool(enemy, "IsAlive"));
                // 选取目标时用得着：并非所有存活敌人都可指定为目标
                w.Prop("hittable", g.Bool(enemy, "IsHittable"));
                WritePowers(w, g, enemy);
                WriteIntents(w, g, monster);
                w.EndObject();
            }
            w.EndArray();
        }

        /// <summary>
        /// 意图 —— 战斗决策最核心的信息。
        ///
        /// 伤害数字存在 <c>DamageCalc : Func&lt;decimal&gt;</c> 里，是延迟计算的
        /// 委托，**必须调用才有值**；`IntentLabelFormat.Variables` 要到渲染时
        /// 才填，实测为空，拿不到现成数字。调用该委托正是游戏 UI 画意图数字
        /// 走的同一条路，无副作用。
        /// </summary>
        private static void WriteIntents(JsonWriter w, Guarded g, object? monster)
        {
            object? nextMove = g.Obj(monster, "NextMove");

            w.BeginArray("intents");
            foreach (var intent in GamePaths.Enumerate(g.Obj(nextMove, "Intents")))
            {
                w.BeginObject();
                w.Prop("type", g.Text(intent, "IntentType"));

                // DamageCalc / Repeats 只存在于攻击类意图，缺失属正常，
                // 故用 TryGet 静默跳过而非记入 warnings。
                if (GamePaths.TryGet(intent, "DamageCalc", out var calc) && calc is Delegate d)
                {
                    decimal? dmg = null;
                    try { dmg = Convert.ToDecimal(d.DynamicInvoke()); }
                    catch (Exception ex) { g.Note($"意图伤害计算失败: {Brief(ex)}"); }
                    w.Prop("damage", dmg);
                }
                if (GamePaths.TryGet(intent, "Repeats", out var rep) && rep is IConvertible c)
                {
                    int repeats = Convert.ToInt32(c);
                    if (repeats > 1) w.Prop("repeats", (int?)repeats);
                }
                w.EndObject();
            }
            w.EndArray();

            // 招式标识便于对照 game-model.md 里实测的具名参数
            w.Prop("move", g.Text(nextMove, "StateId"));
        }

        private static void WritePowers(JsonWriter w, Guarded g, object? creature)
        {
            w.BeginArray("powers");
            foreach (var power in GamePaths.Enumerate(g.Obj(creature, "Powers")))
            {
                w.BeginObject();
                w.Prop("id", GamePaths.Id(power));
                w.Prop("amount", g.Int(power, "Amount"));
                w.Prop("kind", g.Text(power, "Type"));   // Buff / Debuff
                w.EndObject();
            }
            w.EndArray();
        }

        // ------------------------------------------------------------------
        //  手牌与牌堆
        // ------------------------------------------------------------------
        private static void WriteHand(JsonWriter w, Guarded g, object? pcs)
        {
            w.BeginArray("hand");
            int? i = 0;
            foreach (var card in GamePaths.Enumerate(g.Obj(g.Obj(pcs, "Hand"), "Cards")))
            {
                w.BeginObject();
                // 索引即 play_card 的参数，必须与 Hand.Cards 的顺序严格一致
                w.Prop("i", i++);
                w.Prop("id", GamePaths.Id(card));

                // 负费用是「不可打出」的标记而非真实能量消耗（实测诅咒牌
                // AscendersBane 为 -1，且 CostsX 为 false）。对外发 null，
                // 免得模型拿 -1 去做能量运算。
                var cost = g.Int(g.Obj(card, "EnergyCost"), "Canonical");
                w.Prop("cost", cost.HasValue && cost.Value < 0 ? null : cost);

                w.Prop("type", g.Text(card, "Type"));
                w.Prop("target", g.Text(card, "TargetType"));
                WritePlayable(w, g, card);
                var upgrade = g.Int(card, "CurrentUpgradeLevel");
                if (upgrade.HasValue && upgrade.Value > 0) w.Prop("upgraded", upgrade);
                w.EndObject();
            }
            w.EndArray();
        }

        /// <summary>
        /// 「这张牌现在能不能打」。
        ///
        /// 【不能用 IsPlayable】
        /// 实测诅咒牌 AscendersBane 的 IsPlayable 为 true —— 它表达的不是当前
        /// 可否打出。照它来做决策，模型会反复尝试打诅咒牌。
        /// 正确来源是 <c>CanPlay(out UnplayableReason, out AbstractModel)</c>，
        /// 即游戏用来把卡牌置灰的那套判定，能量不足、诅咒、被特定敌人封锁
        /// 全都算在内，并顺带给出原因。
        ///
        /// 原因只在不可打出时输出 —— 常态下不占字节。
        /// </summary>
        private static void WritePlayable(JsonWriter w, Guarded g, object? card)
        {
            // MethodInfo.Invoke 会把 out 参数写回这个数组
            var args = new object?[2];
            var result = g.Invoke(card, "CanPlay", args);

            if (result is bool can)
            {
                w.Prop("playable", can);
                if (!can) w.Prop("reason", args[0]?.ToString());
                return;
            }

            // CanPlay 不可用时退回 IsPlayable，并明确标注该字段不可信 ——
            // 宁可让上层知道判据降级了，也不要悄悄给出错误的可打性。
            w.Prop("playable", g.Bool(card, "IsPlayable"));
            g.Note("CanPlay 不可用，playable 退回 IsPlayable（不区分诅咒与能量不足）");
        }

        private static void WritePiles(JsonWriter w, Guarded g, object? pcs)
        {
            w.BeginObject("piles");
            foreach (var (key, member) in new[]
            {
                ("draw",    "DrawPile"),
                ("discard", "DiscardPile"),
                ("exhaust", "ExhaustPile"),
            })
            {
                // 只发张数不发内容：抽牌堆是乱序的，逐张列出既无决策价值又极占体积
                w.Prop(key, GamePaths.Count(g.Obj(g.Obj(pcs, member), "Cards")));
            }
            w.EndObject();
        }

        // ------------------------------------------------------------------
        //  遗物与药水
        // ------------------------------------------------------------------
        private static void WriteInventory(JsonWriter w, Guarded g, object? player)
        {
            w.BeginArray("relics");
            foreach (var relic in GamePaths.Enumerate(g.Obj(player, "Relics")))
                w.Value(GamePaths.Id(relic));
            w.EndArray();

            // PotionSlots 中空槽为 null，实测 [null, null]。保留 null 占位，
            // 使数组长度即槽位数、下标即 use_potion 的参数。
            w.BeginArray("potions");
            foreach (var potion in GamePaths.Enumerate(g.Obj(player, "PotionSlots")))
                w.Value(GamePaths.Id(potion));
            w.EndArray();
        }

        // ------------------------------------------------------------------

        private static object? FirstOf(object? collection)
        {
            foreach (var item in GamePaths.Enumerate(collection)) return item;
            return null;
        }

        private static string Brief(Exception ex)
        {
            var b = ex.GetBaseException();
            return $"{b.GetType().Name}: {b.Message}";
        }

        /// <summary>
        /// 逐字段容错的读取包装。
        ///
        /// 直接在写 JSON 的过程中 try/catch 会导致结构失衡（异常发生在
        /// BeginObject 与 EndObject 之间，括号对不上）。因此把容错下沉到
        /// 每一次取值：读失败即返回 null 并记一条警告，写入流程本身永不中断。
        ///
        /// 警告去重：同一个成员在每只怪、每张牌上都会失败一次，不去重会刷屏。
        /// </summary>
        private sealed class Guarded
        {
            private readonly HashSet<string> _seen = new HashSet<string>(StringComparer.Ordinal);
            public readonly List<string> Warnings = new List<string>();

            public void Note(string message)
            {
                if (_seen.Add(message)) Warnings.Add(message);
            }

            private T? Run<T>(string label, Func<T?> read)
            {
                try { return read(); }
                catch (Exception ex) { Note($"{label} 读取失败: {Brief(ex)}"); return default; }
            }

            public object? Static(string type, string name) =>
                Run($"{Short(type)}.{name}", () => GamePaths.GetStatic(type, name));

            public object? Obj(object? host, string name) =>
                Run($"{Short(host)}.{name}", () => GamePaths.Get(host, name));

            public int? Int(object? host, string name) =>
                Run($"{Short(host)}.{name}", () => GamePaths.Int(host, name));

            public bool? Bool(object? host, string name) =>
                Run<bool?>($"{Short(host)}.{name}", () => GamePaths.Bool(host, name));

            public string? Text(object? host, string name) =>
                Run($"{Short(host)}.{name}", () => GamePaths.Text(host, name));

            /// <summary>args 数组会被 MethodInfo.Invoke 写回 out 参数，调用方可直接读取。</summary>
            public object? Invoke(object? host, string name, object?[] args) =>
                Run($"{Short(host)}.{name}()", () => GamePaths.Call(host, name, args));

            // 警告里带上宿主类型，游戏更新后能直接定位要改哪个成员
            private static string Short(object? host) => host?.GetType().Name ?? "null";
            private static string Short(string typeFullName)
            {
                int dot = typeFullName.LastIndexOf('.');
                return dot >= 0 ? typeFullName.Substring(dot + 1) : typeFullName;
            }
        }
    }
}
