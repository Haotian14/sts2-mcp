using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace Sts2Bridge
{
    /// <summary>
    /// 极简 JSON 写入器。
    ///
    /// 【为什么不用 System.Text.Json】
    /// 导出的对象没有对应的 C# 类型 —— 每个字段都是反射读出来的 object，
    /// 且大量字段需要「读失败就填 null 并记一条警告」的逐字段容错。
    /// 走序列化器就得先造一层 DTO，DTO 又得跟着游戏版本变，白白多一层。
    /// 直接写字符串反而最短，代价只是逗号容易写错 —— 而逗号正是本类要消掉的。
    ///
    /// 嵌套层级用栈记录「该层是否已写过元素」，由 Begin/End 自动维护分隔符。
    /// </summary>
    internal sealed class JsonWriter
    {
        private readonly StringBuilder _sb = new StringBuilder(4096);
        private readonly Stack<bool> _stack = new Stack<bool>();
        private bool _first = true;

        private void Sep()
        {
            if (!_first) _sb.Append(',');
            _first = false;
        }

        private void Key(string name) => _sb.Append(Reflect.Str(name)).Append(':');

        private void Push()
        {
            _stack.Push(_first);   // _first 此刻已被 Sep() 置为 false，恰是父层的新状态
            _first = true;
        }

        private void Pop() => _first = _stack.Pop();

        public JsonWriter BeginObject()             { Sep(); _sb.Append('{'); Push(); return this; }
        public JsonWriter BeginObject(string name)  { Sep(); Key(name); _sb.Append('{'); Push(); return this; }
        public JsonWriter EndObject()               { _sb.Append('}'); Pop(); return this; }

        public JsonWriter BeginArray(string name)   { Sep(); Key(name); _sb.Append('['); Push(); return this; }
        public JsonWriter EndArray()                { _sb.Append(']'); Pop(); return this; }

        public JsonWriter Prop(string name, string? v)
        {
            Sep(); Key(name); _sb.Append(v == null ? "null" : Reflect.Str(v)); return this;
        }

        public JsonWriter Prop(string name, bool v)
        {
            Sep(); Key(name); _sb.Append(v ? "true" : "false"); return this;
        }

        public JsonWriter Prop(string name, bool? v)
        {
            Sep(); Key(name); _sb.Append(v.HasValue ? (v.Value ? "true" : "false") : "null"); return this;
        }

        public JsonWriter Prop(string name, long v)
        {
            Sep(); Key(name); _sb.Append(v.ToString(CultureInfo.InvariantCulture)); return this;
        }

        /// <summary>可空整数：读失败的字段写 null，而不是伪装成 0 —— 0 是合法血量。</summary>
        public JsonWriter Prop(string name, int? v)
        {
            Sep(); Key(name);
            _sb.Append(v.HasValue ? v.Value.ToString(CultureInfo.InvariantCulture) : "null");
            return this;
        }

        public JsonWriter Prop(string name, decimal? v)
        {
            Sep(); Key(name);
            // 伤害多为整数，去掉无意义的小数尾巴以省字节
            _sb.Append(v.HasValue
                ? (decimal.Truncate(v.Value) == v.Value
                    ? decimal.Truncate(v.Value).ToString(CultureInfo.InvariantCulture)
                    : v.Value.ToString(CultureInfo.InvariantCulture))
                : "null");
            return this;
        }

        /// <summary>
        /// 直接嵌入一段已经成形的 JSON，不做转义。
        /// 用于把 /state 的输出原样塞进动作响应里 —— 状态本就由本类生成，
        /// 反序列化再序列化一遍纯属浪费。传入非法 JSON 会污染整个响应，
        /// 故仅供内部拼装使用。
        /// </summary>
        public JsonWriter Raw(string name, string json)
        {
            Sep(); Key(name); _sb.Append(json); return this;
        }

        /// <summary>数组元素（无键）。</summary>
        public JsonWriter Value(string? v)
        {
            Sep(); _sb.Append(v == null ? "null" : Reflect.Str(v)); return this;
        }

        public override string ToString() => _sb.ToString();
    }
}
