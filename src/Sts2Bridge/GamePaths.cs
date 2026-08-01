using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Reflection;

namespace Sts2Bridge
{
    /// <summary>
    /// 游戏对象的反射访问层 —— 状态导出与动作执行共用的唯一入口。
    ///
    /// 【与 Reflect 的分工】
    /// <see cref="Reflect"/> 是开发期的探索工具：接受任意字符串表达式、每次都
    /// 重新查找、不做缓存，服务于 /eval 与 /describe。
    /// 本类服务于**固化下来的**访问路径：成员一经解析即缓存，每帧可反复调用。
    ///
    /// 【为何仍然全程反射】
    /// 见 csproj 注释：编译期引用游戏程序集会导致其在 Default ALC 中被重复
    /// 加载，游戏必崩。反射拿到的是游戏 ALC 中已存在的那一份。
    ///
    /// 【失效时要能一眼看出是哪里】
    /// 游戏处于抢先体验期，成员随版本消失是常态。所有查找失败都抛出带
    /// 「类型.成员」的 <see cref="MissingMemberException"/>，由调用方转成
    /// warnings 条目 —— 这样游戏更新后看一眼 /state 的 warnings 就知道要改哪。
    /// </summary>
    internal static class GamePaths
    {
        private const BindingFlags Instance =
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
        private const BindingFlags Static =
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static;

        private static readonly ConcurrentDictionary<string, Type?> Types = new();
        private static readonly ConcurrentDictionary<(Type, string), Func<object?, object?>?> Members = new();

        // ------------------------------------------------------------------
        //  类型
        // ------------------------------------------------------------------

        /// <summary>按全名查找游戏类型，结果缓存（含未找到的否定结果）。</summary>
        public static Type? FindType(string fullName) =>
            Types.GetOrAdd(fullName, static n =>
            {
                foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    try
                    {
                        var t = asm.GetType(n, false);
                        if (t != null) return t;
                    }
                    catch { /* 个别程序集反射受限，跳过 */ }
                }
                return null;
            });

        public static Type RequireType(string fullName) =>
            FindType(fullName) ?? throw new MissingMemberException($"找不到类型 {fullName}");

        // ------------------------------------------------------------------
        //  成员读取
        // ------------------------------------------------------------------

        /// <summary>
        /// 解析 host 类型上名为 name 的可读成员（属性优先于字段），沿基类链向上找。
        /// 私有字段也在范围内 —— CombatManager._state 就是私有的。
        /// </summary>
        private static Func<object?, object?> Resolve(Type host, string name) =>
            ResolveOrNull(host, name)
            ?? throw new MissingMemberException($"{host.FullName} 上没有可读成员 {name}");

        private static Func<object?, object?>? ResolveOrNull(Type host, string name)
        {
            return Members.GetOrAdd((host, name), static key =>
            {
                var (t, n) = key;
                for (var cur = t; cur != null; cur = cur.BaseType)
                {
                    var p = cur.GetProperty(n, Instance | Static);
                    if (p != null && p.CanRead)
                    {
                        var getter = p;
                        bool isStatic = p.GetMethod?.IsStatic ?? false;
                        return o => getter.GetValue(isStatic ? null : o);
                    }

                    var f = cur.GetField(n, Instance | Static);
                    if (f != null)
                    {
                        var field = f;
                        bool isStatic = f.IsStatic;
                        return o => field.GetValue(isStatic ? null : o);
                    }
                }
                return null;
            });
        }

        /// <summary>读实例成员。host 为 null 时返回 null，便于链式取值不必层层判空。</summary>
        public static object? Get(object? host, string name) =>
            host == null ? null : Resolve(host.GetType(), name)(host);

        /// <summary>
        /// 成员可能压根不存在时用这个 —— 不存在返回 false 而非抛异常。
        /// 用于多态成员：AttackIntent 有 DamageCalc，其他意图没有，
        /// 这种缺失是正常的，不该污染 warnings。
        /// </summary>
        public static bool TryGet(object? host, string name, out object? value)
        {
            value = null;
            if (host == null) return false;
            var accessor = ResolveOrNull(host.GetType(), name);
            if (accessor == null) return false;
            value = accessor(host);
            return true;
        }

        /// <summary>读静态成员，如 CombatManager.Instance。</summary>
        public static object? GetStatic(string typeFullName, string name) =>
            Resolve(RequireType(typeFullName), name)(null);

        // ------------------------------------------------------------------
        //  方法调用
        //
        //  取值之外还需要调用方法的地方有二：卡面文本必须由游戏自己渲染
        //  （GetDescriptionForPile / LocManager.SmartFormat），以及阶段 3 的
        //  动作下发。按「名称 + 参数个数」缓存 —— 游戏侧同名重载极少，
        //  真遇到时按参数个数已足够区分。
        // ------------------------------------------------------------------

        private static readonly ConcurrentDictionary<(Type, string, int), MethodInfo?> Methods = new();

        private static MethodInfo? FindMethod(Type host, string name, int argCount) =>
            Methods.GetOrAdd((host, name, argCount), static key =>
            {
                var (t, n, count) = key;
                for (var cur = t; cur != null; cur = cur.BaseType)
                {
                    foreach (var m in cur.GetMethods(Instance | Static))
                        if (m.Name == n && m.GetParameters().Length == count)
                            return m;
                }
                return null;
            });

        public static object? Call(object? host, string name, params object?[] args)
        {
            if (host == null) return null;
            var m = FindMethod(host.GetType(), name, args.Length)
                    ?? throw new MissingMethodException($"{host.GetType().FullName} 上没有 {name}（{args.Length} 参数）");
            return m.Invoke(m.IsStatic ? null : host, args);
        }

        /// <summary>
        /// 调用静态类上的方法，如 PlayerCmd.EndTurn —— 这类命令类没有实例，
        /// <see cref="Call"/> 的「由 host 推类型」那条路走不通。
        ///
        /// 注意：反射不会代填可选参数的默认值，有几个形参就得传几个实参。
        /// </summary>
        public static object? CallStatic(string typeFullName, string name, params object?[] args)
        {
            var t = RequireType(typeFullName);
            var m = FindMethod(t, name, args.Length)
                    ?? throw new MissingMethodException($"{typeFullName} 上没有 {name}（{args.Length} 参数）");
            return m.Invoke(null, args);
        }

        /// <summary>枚举值，如 PileType.Hand —— 无法编译期引用游戏的枚举类型。</summary>
        public static object EnumValue(string typeFullName, string name) =>
            Enum.Parse(RequireType(typeFullName), name);

        // ------------------------------------------------------------------
        //  常用类型的取值便捷方法
        //
        //  返回可空类型：读不到时给 null 而非默认值 —— 0 血量与「不知道血量」
        //  必须能区分开，否则模型会照着错误数字做决策。
        // ------------------------------------------------------------------

        public static int? Int(object? host, string name) =>
            Get(host, name) is IConvertible c ? Convert.ToInt32(c) : (int?)null;

        public static bool? Bool(object? host, string name) =>
            Get(host, name) is bool b ? b : (bool?)null;

        /// <summary>枚举与字符串统一转成字符串，供 JSON 直接输出。</summary>
        public static string? Text(object? host, string name) => Get(host, name)?.ToString();

        /// <summary>
        /// 对象的稳定标识 —— 取运行时类型短名，如 StrikeSilent / CorpseSlug。
        ///
        /// 不用本地化的 Title：中文名随语言设置变化，无法在决策日志中跨局比对，
        /// 也无法作为 /cards 字典的键。人类可读的名称由 /cards 单独提供。
        /// </summary>
        public static string? Id(object? model) => model?.GetType().Name;

        /// <summary>
        /// 运行时类型是否派生自某个类型（按短名比对，不必编译期引用）。
        ///
        /// 用于区分「这是张牌还是件遗物」这类多态判断 —— 两者的文本渲染路径
        /// 完全不同（卡牌的 Title 是已渲染 string，遗物的是 LocString）。
        /// </summary>
        public static bool IsA(object? o, string baseTypeName)
        {
            for (var t = o?.GetType(); t != null; t = t.BaseType)
                if (t.Name == baseTypeName) return true;
            return false;
        }

        // ------------------------------------------------------------------
        //  集合
        // ------------------------------------------------------------------

        /// <summary>把成员当作集合枚举；不是集合或为 null 时返回空序列。</summary>
        public static IEnumerable<object?> Items(object? host, string name) =>
            Enumerate(Get(host, name));

        public static IEnumerable<object?> Enumerate(object? collection)
        {
            if (collection is not IEnumerable en || collection is string) yield break;
            foreach (var item in en) yield return item;
        }

        /// <summary>集合的第一个元素；空集合或非集合返回 null。</summary>
        public static object? First(object? collection)
        {
            foreach (var item in Enumerate(collection)) return item;
            return null;
        }

        /// <summary>
        /// 按下标取元素，越界返回 null 而非抛异常 —— 下标来自外部请求，
        /// 越界是可预期的输入错误，应由调用方转成结构化错误回给上层。
        /// </summary>
        public static object? At(object? collection, int index)
        {
            if (index < 0) return null;
            if (collection is IList list) return index < list.Count ? list[index] : null;

            int i = 0;
            foreach (var item in Enumerate(collection))
                if (i++ == index) return item;
            return null;
        }

        public static int? Count(object? collection)
        {
            switch (collection)
            {
                case null: return null;
                case ICollection c: return c.Count;
                case IEnumerable en:
                {
                    int n = 0;
                    foreach (var _ in en) n++;
                    return n;
                }
                default: return null;
            }
        }
    }
}
