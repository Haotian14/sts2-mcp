// ============================================================================
//  Sts2Profiler —— CoreCLR Profiler 注入验证器
//
//  【为什么用 Profiler】
//  DOTNET_STARTUP_HOOKS 对本游戏无效：CoreCLR 的 startup hook 由
//  StartupHookProvider.ProcessStartupHooks() 在 coreclr_execute_assembly 的
//  执行路径中触发，而 Godot 使用 hostfxr 的
//  load_assembly_and_get_function_pointer 加载托管代码，从不调用该函数。
//
//  Profiler 则由 EEStartup 在 CLR 初始化的最早期加载，与 host 的启动路径
//  完全无关 —— 恰好绕开上述死穴。且仅需三个环境变量，不改动游戏目录。
//
//  【本文件的定位：阶段 1.3a，最小验证版】
//  刻意不做任何 IL 注入。只回答两个问题：
//    (1) profiler 到底会不会被 CLR 加载并调用 Initialize？
//    (2) 能否拦截到 sts2.dll 的加载时机？（这是将来 IL 注入的前提）
//  若 (1) 不成立，写再多 IL 重写代码都是白费。
//
//  启用方式（Steam 启动选项）：
//    cmd /C "set CORECLR_ENABLE_PROFILING=1 &&
//            set CORECLR_PROFILER={27585C9F-BB81-4251-B62F-1B463AB4D58A} &&
//            set CORECLR_PROFILER_PATH_64=<本 dll 路径> && %command%"
// ============================================================================

#include <windows.h>
#include <unknwn.h>
#include <cor.h>
#include <corprof.h>

#include <cstdio>
#include <cstdarg>   // va_list / va_start —— Log() 需要，缺失会导致 C2447
#include <cstring>
#include <ctime>
#include <new>
#include <string>

// ---------------------------------------------------------------------------
//  CLSID —— 必须与 CORECLR_PROFILER 环境变量中的 GUID 完全一致
//  {27585C9F-BB81-4251-B62F-1B463AB4D58A}
// ---------------------------------------------------------------------------
static const GUID CLSID_Sts2Profiler =
    { 0x27585c9f, 0xbb81, 0x4251, { 0xb6, 0x2f, 0x1b, 0x46, 0x3a, 0xb4, 0xd5, 0x8a } };

static HMODULE g_hModule = nullptr;
static LONG    g_dllRefs = 0;

// ---------------------------------------------------------------------------
//  日志。profiler 在 CLR 极早期运行，此时没有任何托管设施可用，
//  也没有调试器可附加 —— 日志是唯一的观测手段，因此必须绝对可靠：
//  每次写入都重新打开并 flush，避免进程崩溃时丢失缓冲区内容。
// ---------------------------------------------------------------------------
static void Log(const char* fmt, ...)
{
    char path[MAX_PATH] = {};
    // 不用 SHGetFolderPath，避免在 CLR 早期引入额外依赖
    const char* up = getenv("USERPROFILE");
    if (!up) up = "C:\\Users\\Administrator";
    _snprintf_s(path, sizeof(path), _TRUNCATE,
                "%s\\Desktop\\sts2-mcp\\logs\\profiler.log", up);

    FILE* f = nullptr;
    if (fopen_s(&f, path, "a") != 0 || !f) return;

    SYSTEMTIME st; GetLocalTime(&st);
    fprintf(f, "[%02d:%02d:%02d.%03d] ", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);

    va_list ap; va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);

    fprintf(f, "\n");
    fflush(f);
    fclose(f);
}

// 宽字符转 UTF-8，用于打印模块路径
static std::string ToUtf8(const WCHAR* w)
{
    if (!w) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, nullptr, 0, nullptr, nullptr);
    if (n <= 0) return {};
    std::string s(n - 1, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w, -1, &s[0], n, nullptr, nullptr);
    return s;
}

// ---------------------------------------------------------------------------
//  样板消除宏
//
//  ICorProfilerCallback3 共约 80 个回调方法，最小验证版只关心 Initialize、
//  Shutdown 与 ModuleLoadFinished 三个，其余一律返回 S_OK。
//  注意：绝不能返回 E_NOTIMPL —— CLR 会将其视为错误并卸载 profiler。
// ---------------------------------------------------------------------------
#define NOP(sig) HRESULT STDMETHODCALLTYPE sig override { return S_OK; }

class Sts2Profiler final : public ICorProfilerCallback3
{
public:
    Sts2Profiler() : m_refs(1), m_info(nullptr) {}
    virtual ~Sts2Profiler() { if (m_info) m_info->Release(); }

    // ---- IUnknown ----
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** ppv) override
    {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown ||
            riid == __uuidof(ICorProfilerCallback)  ||
            riid == __uuidof(ICorProfilerCallback2) ||
            riid == __uuidof(ICorProfilerCallback3))
        {
            *ppv = static_cast<ICorProfilerCallback3*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef()  override { return InterlockedIncrement(&m_refs); }
    ULONG STDMETHODCALLTYPE Release() override
    {
        ULONG r = InterlockedDecrement(&m_refs);
        if (r == 0) delete this;
        return r;
    }

    // ---- 真正关心的三个回调 ----

    HRESULT STDMETHODCALLTYPE Initialize(IUnknown* pICorProfilerInfoUnk) override
    {
        Log("========================================================");
        Log("  PROFILER LOADED  —— Initialize 被调用");
        Log("========================================================");
        Log("PID           : %lu", GetCurrentProcessId());

        char exe[MAX_PATH] = {};
        GetModuleFileNameA(nullptr, exe, MAX_PATH);
        Log("宿主进程      : %s", exe);

        if (!pICorProfilerInfoUnk) { Log("!! pICorProfilerInfoUnk 为空"); return E_FAIL; }

        // 逐级降低要求地探测可用的 ICorProfilerInfo 版本 ——
        // 版本号同时也告诉我们这个 CLR 支持到哪一代 profiling API，
        // 直接决定阶段 1.3b 能用哪些 IL 重写接口。
        struct { const IID* iid; const char* name; } probes[] = {
            { &__uuidof(ICorProfilerInfo8), "ICorProfilerInfo8" },
            { &__uuidof(ICorProfilerInfo7), "ICorProfilerInfo7" },
            { &__uuidof(ICorProfilerInfo4), "ICorProfilerInfo4" },
            { &__uuidof(ICorProfilerInfo3), "ICorProfilerInfo3" },
            { &__uuidof(ICorProfilerInfo2), "ICorProfilerInfo2" },
            { &__uuidof(ICorProfilerInfo),  "ICorProfilerInfo"  },
        };
        for (auto& p : probes)
        {
            void* tmp = nullptr;
            if (SUCCEEDED(pICorProfilerInfoUnk->QueryInterface(*p.iid, &tmp)) && tmp)
            {
                Log("可用接口      : %s  [✓]", p.name);
                if (!m_info) m_info = static_cast<ICorProfilerInfo*>(tmp);
                else reinterpret_cast<IUnknown*>(tmp)->Release();
            }
            else
            {
                Log("可用接口      : %s  [✗]", p.name);
            }
        }

        if (!m_info) { Log("!! 未能取得任何 ICorProfilerInfo"); return E_FAIL; }

        // 只订阅模块加载事件。最小验证版不需要 JIT / GC 等高开销事件。
        HRESULT hr = m_info->SetEventMask(COR_PRF_MONITOR_MODULE_LOADS);
        Log("SetEventMask  : hr=0x%08X %s", hr, SUCCEEDED(hr) ? "(成功)" : "(失败)");

        Log("--- 等待模块加载，重点关注 sts2 / GodotSharp / 0Harmony ---");
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE ModuleLoadFinished(ModuleID moduleId, HRESULT hrStatus) override
    {
        if (FAILED(hrStatus) || !m_info) return S_OK;

        WCHAR name[1024] = {};
        ULONG got = 0;
        LPCBYTE base = nullptr;
        AssemblyID asmId = 0;

        if (SUCCEEDED(m_info->GetModuleInfo(moduleId, &base, 1024, &got, name, &asmId)))
        {
            std::string s = ToUtf8(name);

            // 只记录我们关心的三个，否则日志会被数百个 BCL 模块淹没
            for (const char* key : { "sts2.dll", "GodotSharp.dll", "0Harmony.dll" })
            {
                if (s.size() >= strlen(key) &&
                    _stricmp(s.c_str() + s.size() - strlen(key), key) == 0)
                {
                    Log("*** 命中目标模块: %s", s.c_str());
                    Log("    ModuleID=0x%p  AssemblyID=0x%p", (void*)moduleId, (void*)asmId);
                    m_hits++;

                    if (_stricmp(key, "sts2.dll") == 0 && !m_probed)
                    {
                        m_probed = true;
                        ProbeForInjectionSite(moduleId);

                        // 此刻 .cctor 尚未被 JIT，是改写其 IL 的合法时机
                        Log("  [注入] 开始改写 NGame..cctor ...");
                        if (!InjectLoader(moduleId))
                            Log("  [注入] 失败 —— 游戏将以未注入状态继续运行");
                    }
                    break;
                }
            }
        }
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE Shutdown() override
    {
        Log("PROFILER SHUTDOWN —— 命中目标模块 %d 次", m_hits);
        return S_OK;
    }

    // =======================================================================
    //  IL 注入 —— 阶段 1.3b
    //
    //  在 MegaCrit.Sts2.Core.Nodes.NGame..cctor 开头插入 16 字节：
    //
    //      ldstr  "<Sts2Bridge.dll 路径>"          72 <userStringToken>
    //      call   Assembly::LoadFrom(string)       28 <mrLoadFrom>
    //      pop                                     26
    //      call   Sts2Bridge.Entry::Initialize()   28 <mrInitialize>
    //      <原 .cctor 的 27 字节 IL 原样接在后面>
    //
    //  必须先 LoadFrom 再 call：LoadFrom 把桥接程序集载入 Default ALC，
    //  之后 CLR 才能解析那句 call 的目标。若顺序颠倒，程序集解析会按默认
    //  规则去游戏目录找 Sts2Bridge.dll，必然失败。
    //
    //  长度核算：16 + 27 = 43 字节 < Tiny 上限 63，故仍用 1 字节 Tiny header，
    //  无需转 Fat。Tiny 隐含 maxStack = 8，而注入部分最大栈深仅为 1。
    // =======================================================================

    bool InjectLoader(ModuleID moduleId)
    {
        IMetaDataImport2*       mdi     = nullptr;
        IMetaDataEmit2*         emit    = nullptr;
        IMetaDataAssemblyImport* asmImp = nullptr;
        IMetaDataAssemblyEmit*  asmEmit = nullptr;
        bool ok = false;

        // 注意必须带 ofWrite —— 只读句柄无法 emit 新的 TypeRef / MemberRef
        HRESULT hr = m_info->GetModuleMetaData(
            moduleId, ofRead | ofWrite, IID_IMetaDataEmit2, reinterpret_cast<IUnknown**>(&emit));
        if (FAILED(hr) || !emit) { Log("  [注入] 取 IMetaDataEmit2 失败 hr=0x%08X", hr); return false; }

        if (FAILED(emit->QueryInterface(IID_IMetaDataImport2, reinterpret_cast<void**>(&mdi))) ||
            FAILED(emit->QueryInterface(IID_IMetaDataAssemblyImport, reinterpret_cast<void**>(&asmImp))) ||
            FAILED(emit->QueryInterface(IID_IMetaDataAssemblyEmit, reinterpret_cast<void**>(&asmEmit))))
        {
            Log("  [注入] QueryInterface 元数据接口失败");
            goto cleanup;
        }

        {
            // ---- 1. 找到承载 System.Reflection.Assembly 的 AssemblyRef ----
            mdAssemblyRef arCore = mdAssemblyRefNil;
            {
                HCORENUM e = nullptr;
                mdAssemblyRef refs[256] = {};
                ULONG n = 0;
                if (SUCCEEDED(asmImp->EnumAssemblyRefs(&e, refs, 256, &n)))
                {
                    for (ULONG i = 0; i < n && arCore == mdAssemblyRefNil; i++)
                    {
                        WCHAR nm[256] = {}; ULONG nmLen = 0;
                        const void* pk = nullptr; ULONG pkLen = 0;
                        ASSEMBLYMETADATA amd = {};
                        if (SUCCEEDED(asmImp->GetAssemblyRefProps(
                                refs[i], &pk, &pkLen, nm, 256, &nmLen, &amd, nullptr, nullptr, nullptr)))
                        {
                            if (_wcsicmp(nm, L"System.Runtime") == 0 ||
                                _wcsicmp(nm, L"System.Private.CoreLib") == 0)
                            {
                                arCore = refs[i];
                                Log("  [注入] 核心库 AssemblyRef: %s (0x%08X)", ToUtf8(nm).c_str(), arCore);
                            }
                        }
                    }
                }
                if (e) asmImp->CloseEnum(e);
            }
            if (arCore == mdAssemblyRefNil) { Log("  [注入] 未找到 System.Runtime 引用"); goto cleanup; }

            // ---- 2. TypeRef / MemberRef: Assembly::LoadFrom(string) ----
            mdTypeRef trAssembly = mdTypeRefNil;
            if (FAILED(emit->DefineTypeRefByName(arCore, L"System.Reflection.Assembly", &trAssembly)))
            { Log("  [注入] DefineTypeRefByName(Assembly) 失败"); goto cleanup; }

            // 签名: DEFAULT | 1 参 | 返回 CLASS<Assembly> | 参数 STRING
            BYTE sigLoad[16] = {}; ULONG cbLoad = 0;
            sigLoad[cbLoad++] = IMAGE_CEE_CS_CALLCONV_DEFAULT;
            sigLoad[cbLoad++] = 1;
            sigLoad[cbLoad++] = ELEMENT_TYPE_CLASS;
            cbLoad += CorSigCompressToken(trAssembly, &sigLoad[cbLoad]);
            sigLoad[cbLoad++] = ELEMENT_TYPE_STRING;

            mdMemberRef mrLoadFrom = mdMemberRefNil;
            if (FAILED(emit->DefineMemberRef(trAssembly, L"LoadFrom", sigLoad, cbLoad, &mrLoadFrom)))
            { Log("  [注入] DefineMemberRef(LoadFrom) 失败"); goto cleanup; }

            // ---- 3. Assembly::GetType(string) —— 实例方法，返回 System.Type ----
            //
            // 【关键设计约束】注入的 IL 绝不能出现指向 Sts2Bridge 的 call。
            // 方法体是整体 JIT 的：JIT 编译 .cctor 时便会解析其中每一个 call
            // 的目标，该过程发生在同一方法体内的 LoadFrom 执行**之前**，
            // 导致 CLR 按默认探测路径去找 Sts2Bridge.dll 并抛
            // FileNotFoundException。运行时顺序正确，JIT 时序却不正确。
            // 因此对本方 dll 一律走反射，注入的 IL 只引用 BCL 类型。
            mdTypeRef trType = mdTypeRefNil;
            if (FAILED(emit->DefineTypeRefByName(arCore, L"System.Type", &trType)))
            { Log("  [注入] DefineTypeRefByName(Type) 失败"); goto cleanup; }

            BYTE sigGetType[16] = {}; ULONG cbGetType = 0;
            sigGetType[cbGetType++] = IMAGE_CEE_CS_CALLCONV_HASTHIS;   // 实例方法
            sigGetType[cbGetType++] = 1;
            sigGetType[cbGetType++] = ELEMENT_TYPE_CLASS;
            cbGetType += CorSigCompressToken(trType, &sigGetType[cbGetType]);
            sigGetType[cbGetType++] = ELEMENT_TYPE_STRING;

            mdMemberRef mrGetType = mdMemberRefNil;
            if (FAILED(emit->DefineMemberRef(trAssembly, L"GetType", sigGetType, cbGetType, &mrGetType)))
            { Log("  [注入] DefineMemberRef(GetType) 失败"); goto cleanup; }

            // ---- 4. Activator::CreateInstance(Type) —— 触发类型加载与构造 ----
            mdTypeRef trActivator = mdTypeRefNil;
            if (FAILED(emit->DefineTypeRefByName(arCore, L"System.Activator", &trActivator)))
            { Log("  [注入] DefineTypeRefByName(Activator) 失败"); goto cleanup; }

            BYTE sigCreate[16] = {}; ULONG cbCreate = 0;
            sigCreate[cbCreate++] = IMAGE_CEE_CS_CALLCONV_DEFAULT;     // 静态方法
            sigCreate[cbCreate++] = 1;
            sigCreate[cbCreate++] = ELEMENT_TYPE_OBJECT;               // 返回 object
            sigCreate[cbCreate++] = ELEMENT_TYPE_CLASS;
            cbCreate += CorSigCompressToken(trType, &sigCreate[cbCreate]);

            mdMemberRef mrCreate = mdMemberRefNil;
            if (FAILED(emit->DefineMemberRef(trActivator, L"CreateInstance", sigCreate, cbCreate, &mrCreate)))
            { Log("  [注入] DefineMemberRef(CreateInstance) 失败"); goto cleanup; }

            // ---- 5. 用户字符串: dll 绝对路径 与 入口类型全名 ----
            WCHAR bridgePath[MAX_PATH] = {};
            if (!GetBridgeDllPath(bridgePath, MAX_PATH)) { Log("  [注入] 桥接 dll 路径不可用"); goto cleanup; }

            mdString mdPath = mdStringNil;
            if (FAILED(emit->DefineUserString(bridgePath, static_cast<ULONG>(wcslen(bridgePath)), &mdPath)))
            { Log("  [注入] DefineUserString(path) 失败"); goto cleanup; }

            mdString mdTypeName = mdStringNil;
            if (FAILED(emit->DefineUserString(L"Sts2Bridge.Entry", 16, &mdTypeName)))
            { Log("  [注入] DefineUserString(typeName) 失败"); goto cleanup; }

            Log("  [注入] dll: %s", ToUtf8(bridgePath).c_str());
            Log("  [注入] token: LoadFrom=0x%08X GetType=0x%08X CreateInstance=0x%08X",
                mrLoadFrom, mrGetType, mrCreate);

            // ---- 5. 定位 NGame..cctor 并读取原方法体 ----
            mdTypeDef tdNGame = mdTypeDefNil;
            if (FAILED(mdi->FindTypeDefByName(L"MegaCrit.Sts2.Core.Nodes.NGame", mdTokenNil, &tdNGame)))
            { Log("  [注入] 未找到 NGame 类型"); goto cleanup; }

            mdMethodDef mdCctor = mdMethodDefNil;
            if (FAILED(mdi->FindMethod(tdNGame, L".cctor", nullptr, 0, &mdCctor)))
            { Log("  [注入] 未找到 NGame..cctor"); goto cleanup; }

            LPCBYTE oldIl = nullptr; ULONG cbOld = 0;
            if (FAILED(m_info->GetILFunctionBody(moduleId, mdCctor, &oldIl, &cbOld)) || !oldIl)
            { Log("  [注入] 读取 .cctor 方法体失败"); goto cleanup; }

            if ((oldIl[0] & 0x3) != CorILMethod_TinyFormat)
            { Log("  [注入] .cctor 非 Tiny 格式(b0=0x%02X)，本实现不支持", oldIl[0]); goto cleanup; }

            const ULONG oldCode = oldIl[0] >> 2;

            // ---- 6. 拼装新方法体 ----
            // ldstr <path>; call LoadFrom; ldstr "Sts2Bridge.Entry";
            // callvirt GetType; call CreateInstance; pop        —— 共 26 字节
            BYTE inj[32]; ULONG p = 0;
            inj[p++] = 0x72;  memcpy(&inj[p], &mdPath,     4); p += 4;   // ldstr
            inj[p++] = 0x28;  memcpy(&inj[p], &mrLoadFrom, 4); p += 4;   // call
            inj[p++] = 0x72;  memcpy(&inj[p], &mdTypeName, 4); p += 4;   // ldstr
            inj[p++] = 0x6F;  memcpy(&inj[p], &mrGetType,  4); p += 4;   // callvirt
            inj[p++] = 0x28;  memcpy(&inj[p], &mrCreate,   4); p += 4;   // call
            inj[p++] = 0x26;                                             // pop

            const ULONG newCode = p + oldCode;
            if (newCode > 63)
            { Log("  [注入] 新方法体 %lu 字节超出 Tiny 上限，需转 Fat（未实现）", newCode); goto cleanup; }

            {
                IMethodMalloc* mm = nullptr;
                if (FAILED(m_info->GetILFunctionBodyAllocator(moduleId, &mm)) || !mm)
                { Log("  [注入] GetILFunctionBodyAllocator 失败"); goto cleanup; }

                const ULONG total = 1 + newCode;
                BYTE* nb = static_cast<BYTE*>(mm->Alloc(total));
                if (!nb) { Log("  [注入] IMethodMalloc::Alloc 失败"); mm->Release(); goto cleanup; }

                nb[0] = static_cast<BYTE>((newCode << 2) | CorILMethod_TinyFormat);
                memcpy(nb + 1,     inj,        p);
                memcpy(nb + 1 + p, oldIl + 1,  oldCode);   // 跳过原 1 字节 Tiny header

                hr = m_info->SetILFunctionBody(moduleId, mdCctor, nb);
                mm->Release();

                if (FAILED(hr)) { Log("  [注入] SetILFunctionBody 失败 hr=0x%08X", hr); goto cleanup; }

                Log("  [注入] *** 成功 *** NGame..cctor: %lu -> %lu 字节 (header 0x%02X)",
                    oldCode, newCode, nb[0]);
                ok = true;
            }
        }

    cleanup:
        if (asmEmit) asmEmit->Release();
        if (asmImp)  asmImp->Release();
        if (mdi)     mdi->Release();
        if (emit)    emit->Release();
        return ok;
    }

    /// 取得 Sts2Bridge.dll 的绝对路径。
    /// profiler dll 位于 <仓库>\bin\，桥接 dll 位于
    /// <仓库>\src\Sts2Bridge\bin\Release\net9.0\，由前者反推。
    static bool GetBridgeDllPath(WCHAR* out, DWORD cch)
    {
        // 允许用环境变量覆盖，便于调试时指向别处
        if (GetEnvironmentVariableW(L"STS2MCP_BRIDGE_DLL", out, cch) > 0) return true;

        WCHAR self[MAX_PATH] = {};
        if (!GetModuleFileNameW(g_hModule, self, MAX_PATH)) return false;

        WCHAR* slash = wcsrchr(self, L'\\');
        if (!slash) return false;
        *slash = 0;                                   // -> <仓库>\bin
        slash = wcsrchr(self, L'\\');
        if (!slash) return false;
        *slash = 0;                                   // -> <仓库>

        _snwprintf_s(out, cch, _TRUNCATE,
                     L"%s\\src\\Sts2Bridge\\bin\\Release\\net9.0\\Sts2Bridge.dll", self);

        return GetFileAttributesW(out) != INVALID_FILE_ATTRIBUTES;
    }

    // -----------------------------------------------------------------------
    //  元数据侦察 —— 为阶段 1.3b 的 IL 注入寻找落点
    //
    //  理想的注入点需同时满足：
    //    (a) 早期执行（在游戏逻辑跑起来之前）
    //    (b) 静态方法（无需构造 this）
    //    (c) 方法体为 Tiny 格式
    //
    //  (c) 是关键：IL 方法体有 Tiny 与 Fat 两种格式。Fat 格式可携带异常
    //  处理段，其中的 TryOffset / HandlerOffset 是**绝对偏移**；若在方法
    //  开头插入 N 字节，这些偏移全部失效，必须逐一 +N 修正 —— 这是 IL
    //  注入最容易出错、也最容易让游戏崩溃的地方。
    //  Tiny 格式不允许异常段，因此在其开头插入代码时，原 IL 内部的相对
    //  分支偏移保持有效，无需任何修正。
    // -----------------------------------------------------------------------
    void ProbeForInjectionSite(ModuleID moduleId)
    {
        IMetaDataImport2* mdi = nullptr;
        HRESULT hr = m_info->GetModuleMetaData(
            moduleId, ofRead, IID_IMetaDataImport2, reinterpret_cast<IUnknown**>(&mdi));
        if (FAILED(hr) || !mdi) { Log("    [侦察] GetModuleMetaData 失败 hr=0x%08X", hr); return; }

        Log("");
        Log("================ sts2.dll 元数据侦察 ================");

        // <Module> 的 .cctor（模块初始化器）是最理想的落点：CLR 保证它在
        // 模块中任何类型被使用前执行，且注入它无需改动任何游戏方法。
        // 但 C# 只在源码含 [ModuleInitializer] 时才生成它，未必存在。
        ProbeType(moduleId, mdi, L"<Module>");

        for (const WCHAR* t : {
                 L"MegaCrit.Sts2.Core.Nodes.NGame",
                 L"MegaCrit.Sts2.Core.Combat.CombatManager",
                 L"MegaCrit.Sts2.Core.Runs.RunManager",
             })
        {
            ProbeType(moduleId, mdi, t);
        }

        Log("====================================================");
        Log("");
        mdi->Release();
    }

    void ProbeType(ModuleID moduleId, IMetaDataImport2* mdi, const WCHAR* typeName)
    {
        mdTypeDef td = mdTypeDefNil;
        HRESULT hr = mdi->FindTypeDefByName(typeName, mdTokenNil, &td);

        if (FAILED(hr))
        {
            // <Module> 是特殊类型，FindTypeDefByName 未必能查到；
            // 它在元数据中固定为第一个 TypeDef，token 0x02000001。
            if (wcscmp(typeName, L"<Module>") == 0) td = 0x02000001;
            else { Log("  [未找到类型] %s", ToUtf8(typeName).c_str()); return; }
        }

        Log("  类型: %s   (TypeDef=0x%08X)", ToUtf8(typeName).c_str(), td);

        HCORENUM hEnum = nullptr;
        mdMethodDef methods[512] = {};
        ULONG fetched = 0;
        hr = mdi->EnumMethods(&hEnum, td, methods, 512, &fetched);
        if (FAILED(hr) || fetched == 0)
        {
            Log("    (无方法或枚举失败 hr=0x%08X)", hr);
            if (hEnum) mdi->CloseEnum(hEnum);
            return;
        }

        int shown = 0;
        for (ULONG i = 0; i < fetched; i++)
        {
            WCHAR name[512] = {};
            ULONG nameLen = 0;
            DWORD attr = 0, implFlags = 0;
            ULONG rva = 0;
            PCCOR_SIGNATURE sig = nullptr;
            ULONG sigLen = 0;
            mdTypeDef cls = mdTypeDefNil;

            if (FAILED(mdi->GetMethodProps(methods[i], &cls, name, 512, &nameLen,
                                           &attr, &sig, &sigLen, &rva, &implFlags)))
                continue;

            const bool isStatic = (attr & mdStatic) != 0;
            const bool isCctor  = (wcscmp(name, L".cctor") == 0);

            // 只列出静态方法与 .cctor —— 实例方法需要 this，不适合作注入点
            if (!isStatic && !isCctor) continue;

            // 解析 IL 方法体头部
            LPCBYTE il = nullptr;
            ULONG   cbIl = 0;
            const char* fmt = "?";
            ULONG   codeSize = 0;
            bool    hasEh = false;

            const HRESULT hrIl = m_info->GetILFunctionBody(moduleId, methods[i], &il, &cbIl);
            if (SUCCEEDED(hrIl) && il)
            {
                const BYTE b0 = il[0];

                // 【易错点】格式位只取**低 2 位**，不能用 CorILMethod_FormatMask。
                // corhdr.h 中 CorILMethod_FormatMask == 0x7，而 CLR 自身的
                // IMAGE_COR_ILMETHOD_TINY::IsTiny() 用的是 (FormatMask >> 1) == 0x3。
                // 用 0x7 会把 b0=0x1E/0x2E/0x6E 这类（低 2 位为 2，确属 Tiny）
                // 误判成未知格式 —— 大量 .cctor 与 setter 都落在这一类。
                const BYTE fmtBits = b0 & 0x3;

                if (fmtBits == CorILMethod_TinyFormat)
                {
                    fmt = "Tiny";
                    codeSize = b0 >> 2;      // Tiny：高 6 位即代码长度，且不允许异常段
                }
                else if (fmtBits == CorILMethod_FatFormat)
                {
                    fmt = "Fat";
                    const WORD flags = static_cast<WORD>(il[0] | (il[1] << 8)) & 0x0FFF;
                    hasEh    = (flags & CorILMethod_MoreSects) != 0;
                    codeSize = *reinterpret_cast<const DWORD*>(il + 4);
                }
            }

            // 读不出方法体时打印 hr 与 RVA —— 这直接决定该方法能否作为
            // 注入点，不能只显示一个问号了事
            char extra[160] = {};
            if (FAILED(hrIl) || !il)
            {
                _snprintf_s(extra, sizeof(extra), _TRUNCATE,
                            " [读取失败 hr=0x%08X rva=0x%08lX]", hrIl, rva);
            }
            else if (strcmp(fmt, "?") == 0)
            {
                // header 既非 Tiny 也非 Fat —— 打印首字节与 RVA 以判明原因。
                // 若 .cctor 落在此类，需查明它是否可作注入点（.cctor 由 CLR
                // 保证只执行一次，是无需 guard 的理想落点）。
                _snprintf_s(extra, sizeof(extra), _TRUNCATE,
                            " [未知header b0=0x%02X hr=0x%08X rva=0x%08lX attr=0x%04lX implFlags=0x%04lX]",
                            il[0], hrIl, rva, attr, implFlags);
            }
            else if (hasEh)
            {
                _snprintf_s(extra, sizeof(extra), _TRUNCATE, " [含异常段-注入需修正偏移]");
            }
            else if (strcmp(fmt, "Tiny") == 0)
            {
                // 打印完整 IL 字节，用以确认是否为预期的 ldsfld+ret 模式，
                // 注入前必须看到真实字节，不能凭 code size 推断
                char hex[128] = {}; int p = 0;
                const ULONG n = (codeSize < 24 ? codeSize : 24);
                for (ULONG k = 0; k <= n && p < 110; k++)
                    p += _snprintf_s(hex + p, sizeof(hex) - p, _TRUNCATE, "%02X ", il[k]);
                _snprintf_s(extra, sizeof(extra), _TRUNCATE, " [★] IL: %s", hex);
            }

            Log("    %-34s %-6s code=%-5lu %s%s",
                ToUtf8(name).c_str(), fmt, codeSize,
                isStatic ? "static " : "", extra);

            if (++shown >= 25) { Log("    ... (仅列前 25 个)"); break; }
        }

        if (hEnum) mdi->CloseEnum(hEnum);
    }

    // ---- 其余回调：一律 S_OK ----
    NOP(AppDomainCreationStarted(AppDomainID))
    NOP(AppDomainCreationFinished(AppDomainID, HRESULT))
    NOP(AppDomainShutdownStarted(AppDomainID))
    NOP(AppDomainShutdownFinished(AppDomainID, HRESULT))
    NOP(AssemblyLoadStarted(AssemblyID))
    NOP(AssemblyLoadFinished(AssemblyID, HRESULT))
    NOP(AssemblyUnloadStarted(AssemblyID))
    NOP(AssemblyUnloadFinished(AssemblyID, HRESULT))
    NOP(ModuleLoadStarted(ModuleID))
    NOP(ModuleUnloadStarted(ModuleID))
    NOP(ModuleUnloadFinished(ModuleID, HRESULT))
    NOP(ModuleAttachedToAssembly(ModuleID, AssemblyID))
    NOP(ClassLoadStarted(ClassID))
    NOP(ClassLoadFinished(ClassID, HRESULT))
    NOP(ClassUnloadStarted(ClassID))
    NOP(ClassUnloadFinished(ClassID, HRESULT))
    NOP(FunctionUnloadStarted(FunctionID))
    NOP(JITCompilationStarted(FunctionID, BOOL))
    NOP(JITCompilationFinished(FunctionID, HRESULT, BOOL))
    NOP(JITCachedFunctionSearchStarted(FunctionID, BOOL*))
    NOP(JITCachedFunctionSearchFinished(FunctionID, COR_PRF_JIT_CACHE))
    NOP(JITFunctionPitched(FunctionID))
    NOP(JITInlining(FunctionID, FunctionID, BOOL*))
    NOP(ThreadCreated(ThreadID))
    NOP(ThreadDestroyed(ThreadID))
    NOP(ThreadAssignedToOSThread(ThreadID, DWORD))
    NOP(RemotingClientInvocationStarted())
    NOP(RemotingClientSendingMessage(GUID*, BOOL))
    NOP(RemotingClientReceivingReply(GUID*, BOOL))
    NOP(RemotingClientInvocationFinished())
    NOP(RemotingServerReceivingMessage(GUID*, BOOL))
    NOP(RemotingServerInvocationStarted())
    NOP(RemotingServerInvocationReturned())
    NOP(RemotingServerSendingReply(GUID*, BOOL))
    NOP(UnmanagedToManagedTransition(FunctionID, COR_PRF_TRANSITION_REASON))
    NOP(ManagedToUnmanagedTransition(FunctionID, COR_PRF_TRANSITION_REASON))
    NOP(RuntimeSuspendStarted(COR_PRF_SUSPEND_REASON))
    NOP(RuntimeSuspendFinished())
    NOP(RuntimeSuspendAborted())
    NOP(RuntimeResumeStarted())
    NOP(RuntimeResumeFinished())
    NOP(RuntimeThreadSuspended(ThreadID))
    NOP(RuntimeThreadResumed(ThreadID))
    NOP(MovedReferences(ULONG, ObjectID[], ObjectID[], ULONG[]))
    NOP(ObjectAllocated(ObjectID, ClassID))
    NOP(ObjectsAllocatedByClass(ULONG, ClassID[], ULONG[]))
    NOP(ObjectReferences(ObjectID, ClassID, ULONG, ObjectID[]))
    NOP(RootReferences(ULONG, ObjectID[]))
    NOP(ExceptionThrown(ObjectID))
    NOP(ExceptionSearchFunctionEnter(FunctionID))
    NOP(ExceptionSearchFunctionLeave())
    NOP(ExceptionSearchFilterEnter(FunctionID))
    NOP(ExceptionSearchFilterLeave())
    NOP(ExceptionSearchCatcherFound(FunctionID))
    NOP(ExceptionOSHandlerEnter(UINT_PTR))
    NOP(ExceptionOSHandlerLeave(UINT_PTR))
    NOP(ExceptionUnwindFunctionEnter(FunctionID))
    NOP(ExceptionUnwindFunctionLeave())
    NOP(ExceptionUnwindFinallyEnter(FunctionID))
    NOP(ExceptionUnwindFinallyLeave())
    NOP(ExceptionCatcherEnter(FunctionID, ObjectID))
    NOP(ExceptionCatcherLeave())
    NOP(COMClassicVTableCreated(ClassID, REFGUID, void*, ULONG))
    NOP(COMClassicVTableDestroyed(ClassID, REFGUID, void*))
    NOP(ExceptionCLRCatcherFound())
    NOP(ExceptionCLRCatcherExecute())

    // ---- ICorProfilerCallback2 ----
    NOP(ThreadNameChanged(ThreadID, ULONG, WCHAR[]))
    NOP(GarbageCollectionStarted(int, BOOL[], COR_PRF_GC_REASON))
    NOP(SurvivingReferences(ULONG, ObjectID[], ULONG[]))
    NOP(GarbageCollectionFinished())
    NOP(FinalizeableObjectQueued(DWORD, ObjectID))
    NOP(RootReferences2(ULONG, ObjectID[], COR_PRF_GC_ROOT_KIND[], COR_PRF_GC_ROOT_FLAGS[], UINT_PTR[]))
    NOP(HandleCreated(GCHandleID, ObjectID))
    NOP(HandleDestroyed(GCHandleID))

    // ---- ICorProfilerCallback3 ----
    NOP(InitializeForAttach(IUnknown*, void*, UINT))
    NOP(ProfilerAttachComplete())
    NOP(ProfilerDetachSucceeded())

private:
    LONG               m_refs;
    ICorProfilerInfo*  m_info;
    int                m_hits   = 0;
    bool               m_probed = false;   // 元数据侦察只做一次
};

// ---------------------------------------------------------------------------
//  COM 类工厂
// ---------------------------------------------------------------------------
class ClassFactory final : public IClassFactory
{
public:
    ClassFactory() : m_refs(1) {}

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** ppv) override
    {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_IClassFactory)
        {
            *ppv = static_cast<IClassFactory*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef()  override { return InterlockedIncrement(&m_refs); }
    ULONG STDMETHODCALLTYPE Release() override
    {
        ULONG r = InterlockedDecrement(&m_refs);
        if (r == 0) delete this;
        return r;
    }

    HRESULT STDMETHODCALLTYPE CreateInstance(IUnknown* outer, REFIID riid, void** ppv) override
    {
        if (outer) return CLASS_E_NOAGGREGATION;
        Log("ClassFactory::CreateInstance —— CLR 正在请求 profiler 实例");
        auto* p = new (std::nothrow) Sts2Profiler();
        if (!p) return E_OUTOFMEMORY;
        HRESULT hr = p->QueryInterface(riid, ppv);
        p->Release();
        return hr;
    }

    HRESULT STDMETHODCALLTYPE LockServer(BOOL lock) override
    {
        lock ? InterlockedIncrement(&g_dllRefs) : InterlockedDecrement(&g_dllRefs);
        return S_OK;
    }

private:
    LONG m_refs;
};

// ---------------------------------------------------------------------------
//  DLL 导出
// ---------------------------------------------------------------------------
extern "C" BOOL WINAPI DllMain(HMODULE hModule, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH)
    {
        g_hModule = hModule;
        DisableThreadLibraryCalls(hModule);
        // 这行是最早的生命体征：只要它出现，说明 dll 至少被加载进了游戏进程，
        // 即便后续 COM 激活失败也能据此区分故障阶段。
        Log("DllMain: DLL_PROCESS_ATTACH  (pid=%lu)", GetCurrentProcessId());
    }
    return TRUE;
}

extern "C" HRESULT STDAPICALLTYPE DllGetClassObject(REFCLSID rclsid, REFIID riid, void** ppv)
{
    if (!ppv) return E_POINTER;
    if (rclsid != CLSID_Sts2Profiler)
    {
        Log("DllGetClassObject: CLSID 不匹配（环境变量里的 GUID 写错了？）");
        return CLASS_E_CLASSNOTAVAILABLE;
    }
    Log("DllGetClassObject: CLSID 匹配，返回类工厂");
    auto* f = new (std::nothrow) ClassFactory();
    if (!f) return E_OUTOFMEMORY;
    HRESULT hr = f->QueryInterface(riid, ppv);
    f->Release();
    return hr;
}

extern "C" HRESULT STDAPICALLTYPE DllCanUnloadNow()
{
    return (g_dllRefs == 0) ? S_OK : S_FALSE;
}
