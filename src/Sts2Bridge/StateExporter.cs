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
        private const string CombatManager   = "MegaCrit.Sts2.Core.Combat.CombatManager";
        private const string RunManager      = "MegaCrit.Sts2.Core.Runs.RunManager";
        private const string CardPreviewMode = "MegaCrit.Sts2.Core.Entities.Cards.CardPreviewMode";

        /// <summary>
        /// 伤害变量的名字。普通攻击牌是 <c>DamageVar</c>（"Damage"），而条件伤害
        /// 走 <c>CalculatedDamageVar</c>（"CalculatedDamage"）——
        /// 实测「完美打击」只有后者：`{CalculationBase:6, ExtraDamage:2,
        /// CalculatedDamage:22}`，**一个 Damage 键都没有**。
        /// 只认 "Damage" 会让这类牌完全算不出目标侧修正（易伤）。
        /// 按顺序取第一个存在的。
        /// </summary>
        private static readonly string[] DamageKeys = { "Damage", "CalculatedDamage" };

        // CardModel 没有统一的「攻击次数」成员。v0.107.1 的牌实现最终都把次数
        // 作为参数传给 AttackCommand.WithHitCount：有的写死 2，有的取 RepeatVar，
        // 有的现场计算。只列反编译后确认会走 WithHitCount 的牌；未知牌不猜次数，
        // 这样游戏更新后至多低估伤害，不会凭空制造一条假斩杀线。
        private static readonly HashSet<string> FixedDoubleHitCards = new HashSet<string>(StringComparer.Ordinal)
        {
            "AstralPulse", "DaggerSpray", "Maul", "Refract", "RipAndTear",
            "Thrash", "TwinStrike", "Uproar",
        };

        private static readonly HashSet<string> RepeatHitCards = new HashSet<string>(StringComparer.Ordinal)
        {
            "CelestialMight", "Conflagration", "Exterminate", "FightMe", "GunkUp",
            "Peck", "Ricochet", "SevenStars", "SovereignBlade", "SwordBoomerang",
        };

        private static readonly HashSet<string> EnergyXHitCards = new HashSet<string>(StringComparer.Ordinal)
        {
            "Eradicate", "Skewer", "Volley", "Whirlwind",
        };

        private const string HookType = "MegaCrit.Sts2.Core.Hooks.Hook";

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
            // 单机下玩家列表只有一个人。ActionApi 必须取到同一个对象，
            // 否则 /state 给出的手牌下标与 /action 用的对不上。
            object? player = inCombat
                ? GamePaths.First(g.Obj(combatState, "Players"))
                : GamePaths.First(g.Obj(runState, "Players"));

            w.BeginObject();
            w.Prop("ok", true);
            // 重启游戏后停在主菜单，此时没有 RunState —— 除 in_run 外一切皆空。
            // 不明说的话，上层只能从「所有字段都是 null」去猜发生了什么。
            w.Prop("in_run", runState != null);
            if (runState == null)
            {
                try { w.Prop("can_resume", Screens.CanResumeRun); }
                catch (Exception ex) { g.Note($"继续按钮读取失败: {Brief(ex)}"); }
            }
            w.Prop("in_combat", inCombat);

            // 主线程接入状态。降级模式下本次读取发生在 HTTP 线程，理论上可能
            // 跨帧撕裂 —— 数据对不上时先看这里。
            w.Prop("attached", MainThread.IsAttached);
            w.Prop("frame", MainThread.FrameCount);

            // 「游戏正等你做选择」必须出现在 /state 里，不能只在动作响应里。
            // 选择未决期间手牌张数不变（弃牌要确认后才生效），从状态数字根本
            // 推不出来 —— 不明说就只能靠猜，而猜必然会猜错。
            // 此时下发任何动作都会被游戏取消，见 ActionApi.Begin 的前置检查。
            bool awaitingChoice = false;
            string? choiceScreen = null;
            try
            {
                // 顺带确认选择器还装着 —— CardSelectCmd.Reset() 会在一局结束时清空它
                CardChoice.EnsureInstalled();
                awaitingChoice = PlayerChoice.IsPending(out choiceScreen) || CardChoice.IsPending;
            }
            catch (Exception ex) { g.Note($"玩家选择状态读取失败: {Brief(ex)}"); }
            w.Prop("awaiting_choice", awaitingChoice);
            // 在手牌里选的那类没有覆盖界面，screen 为 null 时不发这个字段
            if (choiceScreen != null) w.Prop("screen", choiceScreen);
            // 待答的选牌请求：选项、要选几张。有它就说明可以用 choose 应答；
            // 没有它而 awaiting_choice 为 true，说明是我们接管不了的选择，只能人点。
            try { CardChoice.Describe(w); }
            catch (Exception ex) { g.Note($"选牌选项读取失败: {Brief(ex)}"); }

            // 战斗奖励界面。停在这里时 map.can_move 为 false，得先领完再按继续。
            try { Screens.Describe(w); }
            catch (Exception ex) { g.Note($"奖励界面读取失败: {Brief(ex)}"); }

            WriteRun(w, g, runState, player);

            // 地图。战斗中也发 —— 体积很小，而「下一个节点是精英还是休息点」
            // 会影响本场战斗要留多少血。
            try { MapNav.Describe(w); }
            catch (Exception ex) { g.Note($"地图读取失败: {Brief(ex)}"); }

            // 血量在战斗外同样读得到（实测 RunState.Players[0].Creature.CurrentHp
            // 在结算界面上仍是 31/70），而地图选路、要不要打精英、休息点烤火还是
            // 打铁 —— 每个非战斗决策都要用血量。故不放进 if (inCombat)。
            WritePlayer(w, g, player);

            if (inCombat)
            {
                object? pcs = g.Obj(player, "PlayerCombatState");
                WriteCombat(w, g, combatState, pcs);
                WriteEnemies(w, g, combatState);
                WriteHand(w, g, pcs, combatState);
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
                WriteIntents(w, g, enemy, monster);
                w.EndObject();
            }
            w.EndArray();
        }

        /// <summary>
        /// 意图 —— 战斗决策最核心的信息。
        ///
        /// 【必须用 GetSingleDamage / GetTotalDamage，不能用 DamageCalc】
        /// <c>AttackIntent.DamageCalc : Func&lt;decimal&gt;</c> 给的是**基础伤害**，
        /// 不含力量、虚弱、易伤与遗物修正。游戏画在意图上的数字来自
        /// <c>GetSingleDamage(targets, owner)</c> —— 它内部再走一遍
        /// <c>Hook.ModifyDamage</c>，那才是完整的伤害管线。
        ///
        /// 2026-08-01 实测：噬尸蛞蝓吃掉同伴获得力量 +4 后，`DamageCalc` 仍报
        /// 3×2，而实际掉血 11 = (3+4)×2 − 3 格挡。照 `DamageCalc` 决策会系统性
        /// 少挡 —— 敌人越强、力量越高，低估得越离谱。
        ///
        /// `targets` 参数在 `GetSingleDamage` 内部并未被使用（它按
        /// `LocalContext.GetMe(owner.CombatState)` 自己找目标），故传空数组即可。
        /// </summary>
        private static void WriteIntents(JsonWriter w, Guarded g, object? creature, object? monster)
        {
            object? nextMove = g.Obj(monster, "NextMove");

            // Creature[0]：无法编译期引用游戏类型，只能按运行时类型造数组。
            // 数组协变保证 Creature 的子类数组同样满足 IEnumerable<Creature>。
            Array? noTargets = creature != null
                ? Array.CreateInstance(creature.GetType(), 0)
                : null;

            w.BeginArray("intents");
            foreach (var intent in GamePaths.Enumerate(g.Obj(nextMove, "Intents")))
            {
                w.BeginObject();
                w.Prop("type", g.Text(intent, "IntentType"));

                // 攻击类意图才有 DamageCalc，用它判别类型；非攻击意图没有这些
                // 成员属正常多态缺失，不该污染 warnings。
                if (noTargets != null && GamePaths.TryGet(intent, "DamageCalc", out var calc) && calc is Delegate)
                {
                    // 单次伤害与总伤害都给：决策要总伤害（该挡多少），
                    // 单次伤害则用于判断能否被格挡逐次吃掉。
                    var single = g.Invoke(intent, "GetSingleDamage", new[] { (object?)noTargets, creature });
                    var total  = g.Invoke(intent, "GetTotalDamage",  new[] { (object?)noTargets, creature });

                    if (single is IConvertible sc) w.Prop("damage", (int?)Convert.ToInt32(sc));
                    else
                    {
                        // 兜底：退回基础伤害，并明确标注它不含修正 ——
                        // 宁可让上层知道数字降级了，也不要悄悄给出偏低的伤害
                        decimal? raw = null;
                        try { raw = Convert.ToDecimal(((Delegate)calc).DynamicInvoke()); } catch { }
                        w.Prop("damage", raw);
                        w.Prop("damage_is_base", true);
                        g.Note("GetSingleDamage 不可用，damage 退回 DamageCalc 基础值（不含力量等修正）");
                    }

                    if (total is IConvertible tc) w.Prop("total", (int?)Convert.ToInt32(tc));
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
        private static void WriteHand(JsonWriter w, Guarded g, object? pcs, object? combatState)
        {
            // 算「这张牌打在这只怪身上是多少伤害」要拿敌人当目标，先取一次列表。
            // 顺序与 enemies 数组一致 —— damage_vs 的下标就是 play_card 的目标下标。
            var enemies = new List<object?>(GamePaths.Enumerate(g.Obj(combatState, "Enemies")));

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
                WriteCardValues(w, g, card, enemies);
                w.EndObject();
            }
            w.EndArray();
        }

        /// <summary>
        /// 「这张牌此刻打出去是多少」—— 已代入力量、虚弱、易伤、遗物、附魔的实际数值。
        ///
        /// 【为什么不能拿卡面文本代替】
        /// <c>/glossary</c> 的 <c>GetDescriptionForPile</c> 渲染时用的是
        /// <c>DynamicVar.PreviewValue</c>，而该值**只有先调过
        /// <c>UpdateDynamicVarPreview</c> 才是修正后的数字**，否则等于
        /// <c>BaseValue</c>（游戏界面正是在悬停时先 ClearPreview 再 Update）。
        /// 加之 glossary 一局只取一次，拿它算斩杀线必然是卡面裸值。
        /// 2026-08-01 第一章 Boss 战即因此差 5 点没触发击晕而阵亡（strategy.md §5）。
        ///
        /// 【为什么分 values 与 damage_vs 两块】
        /// 力量、虚弱这类自身修正与目标无关，易伤这类目标侧修正则每只怪各不相同。
        /// 前者放 <c>values</c>，后者只在确实有差异时才发 <c>damage_vs</c> ——
        /// 常态（没人挂易伤）下一个字节都不多花。
        /// </summary>
        private static void WriteCardValues(JsonWriter w, Guarded g, object? card, List<object?> enemies)
        {
            var values = Preview(g, card, null);
            if (values == null || values.Count == 0) return;

            w.BeginObject("values");
            foreach (var kv in values) w.Prop(kv.Key, (int?)kv.Value);
            w.EndObject();

            int? flatHits = HitCount(g, card, values, null);
            // 单段是默认语义，不多发一个字段；0 必须发，否则上层会错当成 1。
            if (flatHits.HasValue && flatHits.Value != 1)
                w.Prop("hits", (int?)flatHits.Value);

            string? key = null;
            int flat = 0;
            foreach (var k in DamageKeys)
                if (values.TryGetValue(k, out flat)) { key = k; break; }
            if (key == null || enemies.Count == 0) return;

            List<int>? perEnemy = new List<int>(enemies.Count);
            List<int?> perEnemyHits = new List<int?>(enemies.Count);
            foreach (var enemy in enemies)
            {
                var v = Preview(g, card, enemy);
                if (v == null || !v.TryGetValue(key, out int d)) { perEnemy = null; break; }
                perEnemy.Add(d);
                perEnemyHits.Add(HitCount(g, card, v, enemy));
            }

            // 复原成无目标的中性预览：游戏界面读的是同一份 PreviewValue，
            // 别让手牌上停着「针对最后一只怪」的数字
            Preview(g, card, null);

            if (perEnemy != null && !perEnemy.TrueForAll(d => d == flat))
            {
                w.BeginArray("damage_vs");
                foreach (var d in perEnemy) w.Value(d);
                w.EndArray();
            }

            // Dismantle 是目前唯一按目标改变次数的牌（易伤目标打两次）。与
            // damage_vs 相同，只有相对无目标预览确有差异时才发送逐敌数组。
            bool hitsDiffer = false;
            if (perEnemyHits.Count == enemies.Count)
            {
                foreach (var h in perEnemyHits)
                {
                    if (!h.HasValue) { hitsDiffer = false; break; }
                    if (h != flatHits) hitsDiffer = true;
                }
            }
            if (hitsDiffer)
            {
                w.BeginArray("hits_vs");
                foreach (var h in perEnemyHits) w.Value(h!.Value);
                w.EndArray();
            }
        }

        /// <summary>
        /// 返回这张牌此刻真正传给 <c>AttackCommand.WithHitCount</c> 的次数。
        /// null 表示反编译清单里没有这张多段牌；1 表示已知是单段，二者不能混淆。
        /// </summary>
        private static int? HitCount(Guarded g, object? card,
            Dictionary<string, int> values, object? target)
        {
            string? id = GamePaths.Id(card);
            if (id == null) return null;

            try
            {
                if (FixedDoubleHitCards.Contains(id)) return 2;

                // CalculatedVar.UpdateCardPreview 已调用它自己的 Calculate(target)，
                // 因而 PreviewValue 就是 OnPlay 随后会取的 CalculatedHits。
                if (values.TryGetValue("CalculatedHits", out int calculated))
                    return calculated;

                if (RepeatHitCards.Contains(id))
                {
                    object? repeat = GamePaths.Get(GamePaths.Get(card, "DynamicVars"), "Repeat");
                    return GamePaths.Int(repeat, "IntValue")
                           ?? (values.TryGetValue("Repeat", out int previewRepeat) ? previewRepeat : (int?)null);
                }

                if (EnergyXHitCards.Contains(id)) return ResolveXValue(card, stars: false);
                if (id == "Stardust") return ResolveXValue(card, stars: true);

                if (id == "HeavenlyDrill")
                {
                    int? hits = ResolveXValue(card, stars: false);
                    object? energyVar = GamePaths.Get(GamePaths.Get(card, "DynamicVars"), "Energy");
                    int? threshold = GamePaths.Int(energyVar, "IntValue");
                    return hits.HasValue && threshold.HasValue && hits.Value >= threshold.Value
                        ? hits.Value * 2
                        : hits;
                }

                if (id == "FiendFire")
                {
                    object? owner = GamePaths.Get(card, "Owner");
                    object? pcs = GamePaths.Get(owner, "PlayerCombatState");
                    int? handCount = GamePaths.Count(GamePaths.Get(GamePaths.Get(pcs, "Hand"), "Cards"));
                    // OnPlay 前 FiendFire 已从手牌移到 Play pile；当前快照里仍在手里。
                    return handCount.HasValue ? Math.Max(0, handCount.Value - 1) : (int?)null;
                }

                if (id == "Dismantle")
                    return target != null && HasPower(target, "VulnerablePower") ? 2 : 1;

                if (id == "Spite")
                {
                    object? owner = GamePaths.Get(card, "Owner");
                    object? creature = GamePaths.Get(owner, "Creature");
                    bool lostHp = GamePaths.Call(card, "LostHpThisTurn", creature) is bool b && b;
                    if (!lostHp) return 1;
                    object? repeat = GamePaths.Get(GamePaths.Get(card, "DynamicVars"), "Repeat");
                    return GamePaths.Int(repeat, "IntValue");
                }
            }
            catch (Exception ex)
            {
                g.Note($"卡牌攻击次数计算失败: {Brief(ex)}");
            }
            return null;
        }

        /// <summary>按出牌当刻可花的能量/星星计算 X，并走 ChemicalX 等修正。</summary>
        private static int? ResolveXValue(object? card, bool stars)
        {
            object? owner = GamePaths.Get(card, "Owner");
            object? pcs = GamePaths.Get(owner, "PlayerCombatState");
            int? original = GamePaths.Int(pcs, stars ? "Stars" : "Energy");
            object? combatState = GamePaths.Get(card, "CombatState");
            if (!original.HasValue || combatState == null) return null;
            object? resolved = GamePaths.CallStatic(HookType, "ModifyXValue", combatState, card, original.Value);
            return resolved is IConvertible c ? Convert.ToInt32(c) : (int?)null;
        }

        private static bool HasPower(object creature, string powerId)
        {
            foreach (var power in GamePaths.Enumerate(GamePaths.Get(creature, "Powers")))
                if (GamePaths.Id(power) == powerId) return true;
            return false;
        }

        /// <summary>
        /// 走一遍游戏自己的预览管线，读出各动态变量的最终值。
        ///
        /// 步骤与 <c>NCardVisuals</c> 刷新卡面时完全一致：先 <c>ClearPreview</c>
        /// 把值退回 base，再 <c>UpdateDynamicVarPreview</c> 过一遍
        /// <c>Hook.ModifyDamage</c>。<c>CardPreviewMode.None</c> 与 <c>Normal</c>
        /// 在这条路径上等价 —— 该参数只对 MultiCreatureTargeting 有意义，
        /// 修正管线本身照跑（与 <c>AttackIntent.GetSingleDamage</c> 的用法一致）。
        ///
        /// 取整用截断而非四舍五入，与卡面显示的 <c>(int)PreviewValue</c> 对齐。
        /// </summary>
        private static Dictionary<string, int>? Preview(Guarded g, object? card, object? target)
        {
            var vars = g.Obj(card, "DynamicVars");
            if (vars == null) return null;

            try
            {
                GamePaths.Call(vars, "ClearPreview");
                GamePaths.Call(card, "UpdateDynamicVarPreview",
                    GamePaths.EnumValue(CardPreviewMode, "None"), target, vars);

                var result = new Dictionary<string, int>(StringComparer.Ordinal);
                foreach (var v in GamePaths.Enumerate(GamePaths.Get(vars, "Values")))
                {
                    var name = GamePaths.Text(v, "Name");
                    if (name != null && GamePaths.Get(v, "PreviewValue") is IConvertible c)
                        result[name] = (int)Convert.ToDecimal(c);
                }
                return result;
            }
            catch (Exception ex)
            {
                // 不带卡牌 id：失败必然是系统性的（游戏更新），带上就会每张牌刷一条
                g.Note($"卡牌实时数值计算失败: {Brief(ex)}");
                return null;
            }
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
