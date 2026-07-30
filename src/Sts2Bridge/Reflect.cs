using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Reflection;
using System.Text;

namespace Sts2Bridge
{
    /// <summary>
    /// 通用反射查询与安全 JSON 转储。
    ///
    /// 【为什么需要它】
    /// 我们对 StS2 的了解仅来自 sts2.xml 的文档注释 —— 它给出类型与方法名，
    /// 却不告诉我们运行时对象的实际形态（字段是否为 null、集合里装的是什么、
    /// 单例何时才被赋值）。若每摸一个字段都要改代码、关游戏、重编译、重进
    /// 一局，迭代成本高得无法接受。
    /// 本类提供在游戏运行期间即时求值任意表达式的能力，摸清结构后再固化为
    /// 正式的状态导出代码。
    ///
    /// 【只读】
    /// 仅读取字段与属性，不调用方法、不写入任何值 —— 探索阶段绝不能因为
    /// 一次误操作而破坏存档或使游戏崩溃。
    /// </summary>
    internal static class Reflect
    {
        private const BindingFlags AllStatic =
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static;
        private const BindingFlags AllInstance =
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;

        // ------------------------------------------------------------------
        //  类型查找
        // ------------------------------------------------------------------
        public static Type? FindType(string fullName)
        {
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type? t = null;
                try { t = asm.GetType(fullName, false); } catch { }
                if (t != null) return t;
            }

            // 退而求其次：按短名匹配（便于手写查询时省略命名空间）
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type[] types;
                try { types = asm.GetTypes(); } catch { continue; }
                foreach (var t in types)
                    if (string.Equals(t.Name, fullName, StringComparison.Ordinal))
                        return t;
            }
            return null;
        }

        // ------------------------------------------------------------------
        //  表达式求值
        //
        //  语法： <类型全名>::<成员>[.<成员>][[索引]]
        //  示例： MegaCrit.Sts2.Core.Runs.RunManager::Instance.CurrentAct
        //         MegaCrit.Sts2.Core.Combat.CombatManager::Instance.Monsters[0]
        //
        //  `::` 之后的第一个成员按静态解析，其余按实例解析。
        // ------------------------------------------------------------------
        public static object? Eval(string expr)
        {
            if (string.IsNullOrWhiteSpace(expr)) throw new ArgumentException("表达式为空");

            var split = expr.Split(new[] { "::" }, StringSplitOptions.None);
            if (split.Length != 2) throw new ArgumentException("表达式须形如 类型全名::成员路径");

            var type = FindType(split[0].Trim())
                       ?? throw new ArgumentException($"找不到类型: {split[0].Trim()}");

            object? current = null;
            bool isStatic = true;

            foreach (var rawSeg in split[1].Split('.'))
            {
                var seg = rawSeg.Trim();
                if (seg.Length == 0) continue;

                // 拆出末尾的 [n] 索引
                int index = -1;
                var br = seg.IndexOf('[');
                if (br >= 0 && seg.EndsWith("]"))
                {
                    var idxText = seg.Substring(br + 1, seg.Length - br - 2);
                    if (!int.TryParse(idxText, out index))
                        throw new ArgumentException($"索引不是整数: {seg}");
                    seg = seg.Substring(0, br);
                }

                if (!isStatic && current == null)
                    throw new InvalidOperationException($"访问 '{seg}' 时其宿主对象为 null");

                var host = isStatic ? type : current!.GetType();
                current = GetMember(host, current, seg, isStatic);
                isStatic = false;

                if (index >= 0) current = GetIndexed(current, index);
            }

            return current;
        }

        private static object? GetMember(Type host, object? instance, string name, bool isStatic)
        {
            var flags = isStatic ? AllStatic : AllInstance;

            for (var t = host; t != null; t = t.BaseType)
            {
                var p = t.GetProperty(name, flags);
                if (p != null && p.CanRead) return p.GetValue(isStatic ? null : instance);

                var f = t.GetField(name, flags);
                if (f != null) return f.GetValue(isStatic ? null : instance);
            }

            throw new MissingMemberException($"{host.FullName} 上找不到可读成员 '{name}'");
        }

        private static object? GetIndexed(object? col, int index)
        {
            if (col == null) throw new InvalidOperationException("对 null 取索引");
            if (col is IList list)
                return index < list.Count ? list[index]
                     : throw new IndexOutOfRangeException($"索引 {index} 越界，长度 {list.Count}");

            if (col is IEnumerable en)
            {
                int i = 0;
                foreach (var item in en) if (i++ == index) return item;
                throw new IndexOutOfRangeException($"索引 {index} 越界，元素数 {i}");
            }
            throw new InvalidOperationException($"{col.GetType().Name} 不可索引");
        }

        // ------------------------------------------------------------------
        //  列出成员 —— 用于探索未知类型的结构
        // ------------------------------------------------------------------
        public static string DescribeType(Type t)
        {
            var sb = new StringBuilder();
            sb.Append('{');
            sb.Append("\"type\":").Append(Str(t.FullName ?? t.Name));
            sb.Append(",\"assembly\":").Append(Str(t.Assembly.GetName().Name ?? "?"));
            sb.Append(",\"base\":").Append(Str(t.BaseType?.FullName ?? ""));

            sb.Append(",\"properties\":[");
            bool first = true;
            foreach (var p in t.GetProperties(AllInstance | AllStatic).Take(200))
            {
                if (!first) sb.Append(',');
                first = false;
                sb.Append('{')
                  .Append("\"name\":").Append(Str(p.Name))
                  .Append(",\"type\":").Append(Str(Simple(p.PropertyType)))
                  .Append(",\"static\":").Append((p.GetMethod?.IsStatic ?? false) ? "true" : "false")
                  .Append('}');
            }
            sb.Append(']');

            sb.Append(",\"fields\":[");
            first = true;
            foreach (var f in t.GetFields(AllInstance | AllStatic).Take(200))
            {
                if (!first) sb.Append(',');
                first = false;
                sb.Append('{')
                  .Append("\"name\":").Append(Str(f.Name))
                  .Append(",\"type\":").Append(Str(Simple(f.FieldType)))
                  .Append(",\"static\":").Append(f.IsStatic ? "true" : "false")
                  .Append('}');
            }
            sb.Append(']');

            sb.Append('}');
            return sb.ToString();
        }

        // ------------------------------------------------------------------
        //  安全 JSON 转储
        //
        //  游戏对象构成的图既深且含循环（Godot 的 Node 树尤甚），朴素的递归
        //  序列化会栈溢出或产生天量输出。这里以三重约束加以限制：
        //  深度上限、集合元素上限、以及基于引用相等的环检测。
        // ------------------------------------------------------------------
        public static string Dump(object? value, int maxDepth = 2, int maxItems = 40)
        {
            var sb = new StringBuilder();
            var seen = new HashSet<object>(ReferenceEqualityComparer.Instance);
            Write(sb, value, maxDepth, maxItems, seen);
            return sb.ToString();
        }

        private static void Write(StringBuilder sb, object? v, int depth, int maxItems, HashSet<object> seen)
        {
            if (v == null) { sb.Append("null"); return; }

            var t = v.GetType();

            if (v is string s)          { sb.Append(Str(s)); return; }
            if (v is bool b)            { sb.Append(b ? "true" : "false"); return; }
            if (t.IsEnum)               { sb.Append(Str(v.ToString() ?? "")); return; }
            if (v is char c)            { sb.Append(Str(c.ToString())); return; }

            if (t.IsPrimitive || v is decimal)
            {
                var d = Convert.ToDouble(v, CultureInfo.InvariantCulture);
                // JSON 无法表示 NaN / Infinity，退化为字符串以免产生非法输出
                sb.Append(double.IsFinite(d)
                    ? d.ToString("R", CultureInfo.InvariantCulture)
                    : Str(v.ToString() ?? ""));
                return;
            }

            if (!t.IsValueType && !seen.Add(v)) { sb.Append("\"<循环引用>\""); return; }

            if (v is IEnumerable en && v is not IDictionary)
            {
                if (depth <= 0) { sb.Append(Str($"<{Simple(t)}>")); return; }
                sb.Append('[');
                int n = 0;
                foreach (var item in en)
                {
                    if (n >= maxItems) { sb.Append(",\"<已截断>\""); break; }
                    if (n++ > 0) sb.Append(',');
                    Write(sb, item, depth - 1, maxItems, seen);
                }
                sb.Append(']');
                return;
            }

            if (depth <= 0) { sb.Append(Str($"<{Simple(t)}>")); return; }

            sb.Append('{').Append("\"$type\":").Append(Str(Simple(t)));

            foreach (var p in t.GetProperties(AllInstance))
            {
                if (p.GetIndexParameters().Length > 0) continue;   // 索引器无法无参读取
                if (!p.CanRead) continue;
                object? pv;
                // 属性 getter 可能因游戏处于特定状态而抛异常，逐个隔离
                try { pv = p.GetValue(v); } catch (Exception ex) { pv = "<抛出 " + ex.GetBaseException().GetType().Name + ">"; }
                sb.Append(',').Append(Str(p.Name)).Append(':');
                Write(sb, pv, depth - 1, maxItems, seen);
            }

            sb.Append('}');
        }

        private static string Simple(Type t)
        {
            if (!t.IsGenericType) return t.FullName ?? t.Name;
            var name = t.Name;
            var tick = name.IndexOf('`');
            if (tick > 0) name = name.Substring(0, tick);
            return name + "<" + string.Join(",", t.GetGenericArguments().Select(a => a.Name)) + ">";
        }

        public static string Str(string s)
        {
            var sb = new StringBuilder(s.Length + 2);
            sb.Append('"');
            foreach (var ch in s)
            {
                switch (ch)
                {
                    case '"':  sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n");  break;
                    case '\r': sb.Append("\\r");  break;
                    case '\t': sb.Append("\\t");  break;
                    default:
                        if (ch < 0x20) sb.Append("\\u").Append(((int)ch).ToString("x4"));
                        else sb.Append(ch);
                        break;
                }
            }
            sb.Append('"');
            return sb.ToString();
        }

        private sealed class ReferenceEqualityComparer : IEqualityComparer<object>
        {
            public static readonly ReferenceEqualityComparer Instance = new ReferenceEqualityComparer();
            public new bool Equals(object? a, object? b) => ReferenceEquals(a, b);
            public int GetHashCode(object o) => System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(o);
        }
    }
}
