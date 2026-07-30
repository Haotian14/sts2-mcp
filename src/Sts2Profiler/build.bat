@echo off
REM ===========================================================================
REM  构建 Sts2Profiler.dll (x64)
REM
REM  必须是 x64 —— 游戏是 64 位进程，位数不匹配时 CLR 会静默拒绝加载
REM  profiler，且不会给出任何错误提示，极难排查。
REM ===========================================================================
setlocal

set VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe
if not exist "%VSWHERE%" (
    echo [错误] 找不到 vswhere.exe —— VS Build Tools 未安装
    exit /b 1
)

for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set VSPATH=%%i

if not defined VSPATH (
    echo [错误] 未找到含 C++ 工具集的 VS 安装
    exit /b 1
)

echo [信息] VS 路径: %VSPATH%
call "%VSPATH%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 ( echo [错误] vcvars64.bat 调用失败 & exit /b 1 )

set OUTDIR=%~dp0..\..\bin
if not exist "%OUTDIR%" mkdir "%OUTDIR%"

pushd "%~dp0"

REM /MT : 静态链接 CRT。profiler 在 CLR 极早期加载，此时进程中可用的
REM       运行时环境很有限，动态 CRT 依赖会带来不必要的加载失败风险。
cl /nologo /LD /EHsc /O2 /MT /std:c++17 /W3 ^
   Sts2Profiler.cpp ^
   /Fe:"%OUTDIR%\Sts2Profiler.dll" ^
   /Fo:"%OUTDIR%\\" ^
   /link /DEF:Sts2Profiler.def /MACHINE:X64

set RC=%errorlevel%
popd

if %RC% neq 0 ( echo [失败] 编译错误 & exit /b %RC% )

echo.
echo [成功] 输出: %OUTDIR%\Sts2Profiler.dll
exit /b 0
