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
#include <ctime>
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
                    if (m_hits >= 1)
                        Log("    => 可在此时机执行 IL 注入（阶段 1.3b 的落点）");
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
    int                m_hits = 0;
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
