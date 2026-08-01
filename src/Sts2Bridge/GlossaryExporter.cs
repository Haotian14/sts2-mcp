using System;
using System.Collections.Generic;

namespace Sts2Bridge
{
    /// <summary>
    /// 卡面文本字典 —— 与 <see cref="StateExporter"/> 配套的静态那一半。
    ///
    /// 【为什么要拆成两个接口】
    /// 卡牌描述、遗物说明这类文本每回合一字不变，却是载荷里最大的一块。
    /// 塞进 /state 就等于每回合重传一遍相同文本，token 成本按回合数线性增长。
    /// 拆出来之后 MCP server 一局只取一次并缓存，/state 只发会变的数字。
    ///
    /// 【为什么不按 id 查询】
    /// 描述必须由游戏侧的活对象渲染 —— 数值要代入（「造成 6 点伤害」中的 6
    /// 受力量、遗物、升级影响），光有 id 字符串无从渲染。因此本接口直接遍历
    /// 玩家当前**实际持有**的牌与遗物，整体返回；这恰好也正是 MCP server
    /// 需要的全集。
    /// </summary>
    internal static class GlossaryExporter
    {
        private const string CombatManager = "MegaCrit.Sts2.Core.Combat.CombatManager";
        private const string RunManager    = "MegaCrit.Sts2.Core.Runs.RunManager";
        private const string LocManager    = "MegaCrit.Sts2.Core.Localization.LocManager";
        private const string PileType      = "MegaCrit.Sts2.Core.Entities.Cards.PileType";

        public static string Export()
        {
            var w = new JsonWriter();
            var warnings = new List<string>();
            var seen = new HashSet<string>(StringComparer.Ordinal);

            object? combat = TryStatic(CombatManager, "Instance", warnings);
            object? run    = TryStatic(RunManager, "Instance", warnings);

            bool inCombat = combat != null && Get(combat, "IsInProgress") is true;
            object? player = inCombat
                ? First(Get(Get(combat, "_state"), "Players"))
                : First(Get(Get(run, "State"), "Players"));

            w.BeginObject();
            w.Prop("ok", true);

            // 牌库是全集；战斗中生成的临时牌（如「暗影」）不在牌库里，故并上手牌
            w.BeginObject("cards");
            seen.Clear();
            foreach (var card in AllCards(player, inCombat))
                WriteCard(w, card, seen, warnings);
            w.EndObject();

            w.BeginObject("relics");
            seen.Clear();
            foreach (var relic in GamePaths.Enumerate(Get(player, "Relics")))
                WriteLocalized(w, relic, "Title", "DynamicDescription", seen, warnings);
            w.EndObject();

            w.BeginObject("potions");
            seen.Clear();
            foreach (var potion in GamePaths.Enumerate(Get(player, "PotionSlots")))
                WriteLocalized(w, potion, "Title", "Description", seen, warnings);
            w.EndObject();

            // 场上出现的增益/减益 —— 模型需要知道「贪食」到底是什么效果
            w.BeginObject("powers");
            seen.Clear();
            if (inCombat)
                foreach (var creature in GamePaths.Enumerate(Get(Get(combat, "_state"), "Creatures")))
                foreach (var power in GamePaths.Enumerate(Get(creature, "Powers")))
                    WriteLocalized(w, power, "Title", "Description", seen, warnings);
            w.EndObject();

            w.BeginArray("warnings");
            foreach (var msg in warnings) w.Value(msg);
            w.EndArray();

            w.EndObject();
            return w.ToString();
        }

        // ------------------------------------------------------------------

        private static IEnumerable<object?> AllCards(object? player, bool inCombat)
        {
            foreach (var c in GamePaths.Enumerate(Get(Get(player, "Deck"), "Cards")))
                yield return c;

            if (!inCombat) yield break;
            foreach (var c in GamePaths.Enumerate(Get(Get(player, "PlayerCombatState"), "AllCards")))
                yield return c;
        }

        /// <summary>
        /// 卡牌文本由 <c>GetDescriptionForPile</c> 渲染 —— 它输出的是代入了
        /// **卡面数值**的最终文本（含升级），传 Hand 与 null 目标，目标相关的
        /// 描述退化为通用措辞。
        ///
        /// ⚠️ 这里的数字**不含力量、虚弱、易伤、遗物等战斗中的修正**。
        /// 渲染读的是 <c>DynamicVar.PreviewValue</c>，而该值只有先调过
        /// <c>UpdateDynamicVarPreview</c> 才是修正后的数字，否则等于 BaseValue；
        /// 何况本接口一局只取一次缓存，天然给不出「此刻」的值。
        /// 需要实际数值请用 <c>/state</c> 的 <c>hand[].values</c>（见
        /// <see cref="StateExporter"/> 的 WriteCardValues）。
        /// </summary>
        private static void WriteCard(JsonWriter w, object? card, HashSet<string> seen, List<string> warnings)
        {
            var id = GamePaths.Id(card);
            if (id == null || !seen.Add(id)) return;

            w.BeginObject(id);
            w.Prop("title", Safe(() => GamePaths.Text(card, "Title"), $"{id}.Title", warnings));
            w.Prop("text", StripMarkup(Safe(() => (string?)GamePaths.Call(
                        card, "GetDescriptionForPile", GamePaths.EnumValue(PileType, "Hand"), null),
                    $"{id}.GetDescriptionForPile", warnings)));
            w.Prop("cost", Safe(() => GamePaths.Int(GamePaths.Get(card, "EnergyCost"), "Canonical"),
                    $"{id}.EnergyCost", warnings));
            w.Prop("type", Safe(() => GamePaths.Text(card, "Type"), $"{id}.Type", warnings));
            w.Prop("target", Safe(() => GamePaths.Text(card, "TargetType"), $"{id}.TargetType", warnings));
            w.EndObject();
        }

        /// <summary>遗物 / 药水 / 增益：标题与描述都是未渲染的 LocString，需过本地化器。</summary>
        private static void WriteLocalized(
            JsonWriter w, object? model, string titleMember, string textMember,
            HashSet<string> seen, List<string> warnings)
        {
            var id = GamePaths.Id(model);
            if (id == null || !seen.Add(id)) return;   // 空药水槽为 null，Id 亦为 null，自然跳过

            w.BeginObject(id);
            w.Prop("title", StripMarkup(Safe(() => Render(GamePaths.Get(model, titleMember)), $"{id}.{titleMember}", warnings)));
            w.Prop("text",  StripMarkup(Safe(() => Render(GamePaths.Get(model, textMember)),  $"{id}.{textMember}",  warnings)));
            w.EndObject();
        }

        // ------------------------------------------------------------------
        //  给 Screens 复用的单个模型渲染
        //
        //  本类导出的是**已持有**的牌与遗物，而卡牌三选一、商店、Boss 遗物
        //  给的是**待选**的东西 —— 后者不在牌库里，本接口一个都覆盖不到。
        //  文本又不能塞进 /glossary：那是一局取一次的静态缓存，而候选物每个
        //  界面都不一样，塞进去要么发过期数据、要么每次重取整副牌组。
        //  故把渲染逻辑开放出去，由 Screens 就地写进 screen.options[]。
        // ------------------------------------------------------------------

        /// <summary>
        /// 模型的显示名。卡牌的 <c>Title</c> 已是渲染好的 string，
        /// 遗物/药水的是 LocString，得过一遍本地化器 —— 两者不一致，见 game-model.md。
        /// </summary>
        internal static string? TitleOf(object? model, List<string>? warnings = null)
        {
            if (model == null) return null;
            var id = GamePaths.Id(model);

            if (GamePaths.IsA(model, "CardModel"))
                return StripMarkup(Safe(() => GamePaths.Text(model, "Title"), $"{id}.Title", warnings));

            return StripMarkup(Safe(() => Render(GamePaths.Get(model, "Title")), $"{id}.Title", warnings));
        }

        /// <summary>
        /// 模型的效果文本。
        ///
        /// 遗物的描述成员叫 <c>DynamicDescription</c>、药水的叫 <c>Description</c>，
        /// 按存在性依次尝试 —— 这是正常的多态差异，不该记成警告。
        /// </summary>
        internal static string? TextOf(object? model, List<string>? warnings = null)
        {
            if (model == null) return null;
            var id = GamePaths.Id(model);

            if (GamePaths.IsA(model, "CardModel"))
                return StripMarkup(Safe(() => (string?)GamePaths.Call(
                        model, "GetDescriptionForPile", GamePaths.EnumValue(PileType, "Hand"), null),
                    $"{id}.GetDescriptionForPile", warnings));

            foreach (var member in new[] { "DynamicDescription", "Description" })
                if (GamePaths.TryGet(model, member, out var loc) && loc != null)
                    return StripMarkup(Safe(() => Render(loc), $"{id}.{member}", warnings));

            return null;
        }

        /// <summary>
        /// LocString → 文本。LocString 自身只持有表名与键（如 relics /
        /// RING_OF_THE_SNAKE.description），真正的查表与变量代入由 LocManager 完成。
        /// </summary>
        private static string? Render(object? locString)
        {
            if (locString == null) return null;
            var lm = GamePaths.GetStatic(LocManager, "Instance");
            if (lm == null) return null;
            return (string?)GamePaths.Call(lm, "SmartFormat", locString, GamePaths.Get(locString, "Variables"));
        }

        // ------------------------------------------------------------------

        /// <summary>
        /// 剥掉 Godot 的 BBCode 着色标记。
        ///
        /// 渲染出来的文本形如「获得5点[gold]格挡[/gold]」—— 方括号部分是给
        /// RichTextLabel 上色用的，对模型没有任何信息量，却占了可观的比例。
        /// 关键词本身（格挡、中毒）保留，丢掉的只是颜色。
        ///
        /// 仅剥离形如 [tag] / [/tag] / [tag=value] 的标记；含空格或非标记字符的
        /// 方括号原样保留，避免误伤正文里可能出现的括号。
        ///
        /// 【[img] 要连内容一起丢】
        /// 着色标记之间的是正文（`[gold]格挡[/gold]` → `格挡`），但 [img] 之间
        /// 的是资源路径，不是给人看的：
        ///
        ///   耗能变为0[img]res://images/…/ironclad_energy_icon.png[/img]。
        ///
        /// 只剥标签会留下一串裸路径，既是噪音又会把「变为 0」和后面的句子黏成
        /// 一句看不懂的话。2026-08-01 在商店的「无情猛攻」上暴露出来。
        /// </summary>
        internal static string? StripMarkup(string? text)
        {
            if (string.IsNullOrEmpty(text) || text!.IndexOf('[') < 0) return text;

            var sb = new System.Text.StringBuilder(text.Length);
            int i = 0;
            while (i < text.Length)
            {
                if (text[i] == '[')
                {
                    int close = text.IndexOf(']', i + 1);
                    if (close > i && IsMarkupTag(text, i + 1, close))
                    {
                        // 图片：连同中间的路径与闭合标记一并丢弃。
                        // 找不到闭合标记时退回只剥这一个标签，绝不吞掉后面的正文。
                        if (IsTagNamed(text, i + 1, close, "img"))
                        {
                            int end = text.IndexOf("[/img]", close + 1, StringComparison.OrdinalIgnoreCase);
                            i = end >= 0 ? end + "[/img]".Length : close + 1;
                            continue;
                        }
                        i = close + 1;
                        continue;
                    }
                }
                sb.Append(text[i++]);
            }
            return sb.ToString();
        }

        /// <summary>标记的名字是否为 name（忽略闭合斜杠与 =value 部分）。</summary>
        private static bool IsTagNamed(string s, int start, int end, string name)
        {
            if (start < end && s[start] == '/') start++;
            int stop = s.IndexOf('=', start);
            if (stop < 0 || stop > end) stop = end;
            return stop - start == name.Length &&
                   string.Compare(s, start, name, 0, name.Length, StringComparison.OrdinalIgnoreCase) == 0;
        }

        private static bool IsMarkupTag(string s, int start, int end)
        {
            if (end <= start) return false;
            if (s[start] == '/') start++;            // 闭合标记
            if (start >= end || !char.IsLetter(s[start])) return false;

            for (int i = start; i < end; i++)
            {
                char c = s[i];
                if (!char.IsLetterOrDigit(c) && c != '_' && c != '=' && c != '#') return false;
            }
            return true;
        }

        private static object? Get(object? host, string name)
        {
            try { return GamePaths.Get(host, name); } catch { return null; }
        }

        private static object? TryStatic(string type, string name, List<string> warnings)
        {
            try { return GamePaths.GetStatic(type, name); }
            catch (Exception ex) { warnings.Add($"{type}.{name} 读取失败: {ex.GetBaseException().Message}"); return null; }
        }

        /// <summary>warnings 为 null 时静默吞掉 —— Screens 那一侧没有警告通道。</summary>
        private static T? Safe<T>(Func<T?> read, string label, List<string>? warnings)
        {
            try { return read(); }
            catch (Exception ex)
            {
                if (warnings == null) return default;
                var msg = $"{label} 失败: {ex.GetBaseException().Message}";
                if (!warnings.Contains(msg)) warnings.Add(msg);
                return default;
            }
        }

        private static object? First(object? collection)
        {
            foreach (var item in GamePaths.Enumerate(collection)) return item;
            return null;
        }
    }
}
