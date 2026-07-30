using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;

namespace Sts2Bridge
{
    /// <summary>
    /// 游戏进程内的本地 HTTP 服务，供 MCP server 查询状态与下发动作。
    ///
    /// 【为何自己写而不用 HttpListener】
    /// HttpListener 依赖 HTTP.sys，非管理员进程绑定端口前缀往往需要预先用
    /// netsh 注册 urlacl，否则抛 HttpListenerException(5)。游戏由 Steam 以
    /// 普通权限启动，不能要求用户额外配置。TcpListener 无此限制，而我们只
    /// 需要 GET/POST + JSON 这点最小子集，自行实现更可靠。
    ///
    /// 【只绑 127.0.0.1】
    /// 该接口可读取并操纵游戏状态，绝不可暴露到网络接口上。
    /// </summary>
    internal static class HttpApi
    {
        private static TcpListener? _listener;
        private static Thread? _thread;
        private static volatile bool _running;

        public const int DefaultPort = 8765;
        public static int Port { get; private set; }

        /// <summary>
        /// 解析监听端口。优先取环境变量 STS2MCP_PORT，便于多开或端口冲突时调整；
        /// 未设置或非法时回落到 <see cref="DefaultPort"/>。
        /// 之所以用环境变量而非配置文件：桥接层运行在游戏进程内，启动时序早于
        /// 一切，读文件需处理路径解析与失败回退；而环境变量本就要由 Steam 启动
        /// 脚本注入（profiler 三件套已走这条路），顺带传一个端口零成本。
        /// </summary>
        public static int ResolvePort()
        {
            var raw = Environment.GetEnvironmentVariable("STS2MCP_PORT");
            if (int.TryParse(raw, out var p) && p > 0 && p < 65536) return p;
            return DefaultPort;
        }

        public static void Start(int? explicitPort = null)
        {
            var port = explicitPort ?? ResolvePort();
            try
            {
                _listener = new TcpListener(IPAddress.Loopback, port);
                _listener.Start();
                Port = port;
                _running = true;

                _thread = new Thread(AcceptLoop)
                {
                    IsBackground = true,          // 不阻止游戏退出
                    Name = "sts2-mcp-http",
                };
                _thread.Start();

                Log.Write($"[HTTP] 已监听 http://127.0.0.1:{port}/");
            }
            catch (Exception ex)
            {
                Log.Error($"HTTP 启动失败 (端口 {port})", ex);
            }
        }

        private static void AcceptLoop()
        {
            while (_running)
            {
                try
                {
                    var client = _listener!.AcceptTcpClient();
                    // 每个连接单独处理；请求本身很少，无需线程池优化
                    var t = new Thread(() => Handle(client)) { IsBackground = true };
                    t.Start();
                }
                catch (Exception ex)
                {
                    if (_running) Log.Error("HTTP accept", ex);
                }
            }
        }

        private static void Handle(TcpClient client)
        {
            try
            {
                using (client)
                using (var stream = client.GetStream())
                {
                    client.ReceiveTimeout = 5000;
                    client.SendTimeout = 5000;

                    var (method, path, query, body) = ReadRequest(stream);
                    if (method == null) return;

                    string json;
                    int status = 200;
                    try
                    {
                        json = Route(method, path!, query!, body);
                    }
                    catch (Exception ex)
                    {
                        status = 400;
                        json = "{\"ok\":false,\"error\":" + Reflect.Str(ex.GetBaseException().Message)
                             + ",\"type\":" + Reflect.Str(ex.GetBaseException().GetType().Name) + "}";
                    }

                    var payload = Encoding.UTF8.GetBytes(json);
                    var head = Encoding.ASCII.GetBytes(
                        $"HTTP/1.1 {status} {(status == 200 ? "OK" : "Bad Request")}\r\n" +
                        "Content-Type: application/json; charset=utf-8\r\n" +
                        $"Content-Length: {payload.Length}\r\n" +
                        "Connection: close\r\n\r\n");

                    stream.Write(head, 0, head.Length);
                    stream.Write(payload, 0, payload.Length);
                    stream.Flush();
                }
            }
            catch (Exception ex)
            {
                Log.Error("HTTP 处理", ex);
            }
        }

        private static (string?, string?, Dictionary<string, string>?, string?) ReadRequest(NetworkStream stream)
        {
            var header = new StringBuilder();
            var one = new byte[1];

            // 逐字节读到空行为止。请求头很小，无需缓冲优化；
            // 逐字节可确保不会多读走 body 的字节。
            while (!header.ToString().EndsWith("\r\n\r\n"))
            {
                int n = stream.Read(one, 0, 1);
                if (n <= 0) return (null, null, null, null);
                header.Append((char)one[0]);
                if (header.Length > 16384) return (null, null, null, null);
            }

            var lines = header.ToString().Split(new[] { "\r\n" }, StringSplitOptions.None);
            var parts = lines[0].Split(' ');
            if (parts.Length < 2) return (null, null, null, null);

            var method = parts[0];
            var rawUrl = parts[1];

            var q = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            var path = rawUrl;
            var qm = rawUrl.IndexOf('?');
            if (qm >= 0)
            {
                path = rawUrl.Substring(0, qm);
                foreach (var kv in rawUrl.Substring(qm + 1).Split('&'))
                {
                    if (kv.Length == 0) continue;
                    var eq = kv.IndexOf('=');
                    if (eq < 0) q[Uri.UnescapeDataString(kv)] = "";
                    else q[Uri.UnescapeDataString(kv.Substring(0, eq))] =
                             Uri.UnescapeDataString(kv.Substring(eq + 1).Replace('+', ' '));
                }
            }

            string? body = null;
            int len = 0;
            foreach (var line in lines)
                if (line.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase))
                    int.TryParse(line.Substring(15).Trim(), out len);

            if (len > 0)
            {
                var buf = new byte[len];
                int got = 0;
                while (got < len)
                {
                    int n = stream.Read(buf, got, len - got);
                    if (n <= 0) break;
                    got += n;
                }
                body = Encoding.UTF8.GetString(buf, 0, got);
            }

            return (method, path, q, body);
        }

        // ------------------------------------------------------------------
        //  路由
        //
        //  凡是触碰游戏对象的处理，一律经 MainThread.RunSync 投递到主线程 ——
        //  当前线程是 HTTP 处理线程，直接读游戏状态会读到撕裂数据甚至崩溃。
        // ------------------------------------------------------------------
        private static string Route(string method, string path, Dictionary<string, string> q, string? body)
        {
            switch (path)
            {
                case "/health":
                    return "{\"ok\":true,\"attached\":" + (MainThread.IsAttached ? "true" : "false")
                         + ",\"frame\":" + MainThread.FrameCount
                         + ",\"pid\":" + Environment.ProcessId + "}";

                // 即时求值任意只读表达式，用于摸清运行时对象结构。
                // 例：/eval?expr=MegaCrit.Sts2.Core.Runs.RunManager::Instance.CurrentAct
                case "/eval":
                {
                    if (!q.TryGetValue("expr", out var expr))
                        throw new ArgumentException("缺少参数 expr");
                    int depth = q.TryGetValue("depth", out var d) && int.TryParse(d, out var dv) ? dv : 2;

                    var dump = MainThread.RunSync(() => Reflect.Dump(Reflect.Eval(expr), depth));
                    return "{\"ok\":true,\"expr\":" + Reflect.Str(expr) + ",\"value\":" + dump + "}";
                }

                // 列出类型的属性与字段，用于发现可用成员
                case "/describe":
                {
                    if (!q.TryGetValue("type", out var tn))
                        throw new ArgumentException("缺少参数 type");
                    var t = Reflect.FindType(tn) ?? throw new ArgumentException($"找不到类型: {tn}");
                    return "{\"ok\":true,\"info\":" + Reflect.DescribeType(t) + "}";
                }

                default:
                    throw new ArgumentException($"未知路径: {path}");
            }
        }
    }
}
