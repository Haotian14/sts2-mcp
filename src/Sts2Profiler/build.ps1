<#
.SYNOPSIS
    构建 Sts2Profiler.dll (x64)。

.NOTES
    必须是 x64 —— 游戏是 64 位进程。位数不匹配时 CLR 会静默拒绝加载
    profiler，且不给出任何错误提示，极难排查。
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$Here   = $PSScriptRoot
$OutDir = Join-Path (Split-Path (Split-Path $Here -Parent) -Parent) 'bin'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# --- 定位 MSVC ------------------------------------------------------------
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw "找不到 vswhere.exe —— VS Build Tools 未安装" }

$vsPath = & $vswhere -latest -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -property installationPath
if (-not $vsPath) { throw "未找到含 C++ 工具集的 VS 安装" }

$vcvars = Join-Path $vsPath 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path $vcvars)) { throw "找不到 vcvars64.bat: $vcvars" }

Write-Host "[信息] VS: $vsPath"

# --- 把 vcvars 的环境变量导入当前 PowerShell 会话 --------------------------
# vcvars64.bat 只影响它自己的 cmd 进程，必须把结果 set 回来才能在此调用 cl。
& cmd /c "call `"$vcvars`" >nul 2>&1 && set" | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)$') {
        Set-Item -Path "Env:\$($matches[1])" -Value $matches[2] -ErrorAction SilentlyContinue
    }
}

$cl = Get-Command cl.exe -ErrorAction SilentlyContinue
if (-not $cl) { throw "vcvars 导入后仍找不到 cl.exe" }
Write-Host "[信息] cl: $($cl.Source)"

# --- 编译 -----------------------------------------------------------------
Push-Location $Here
try {
    $clArgs = @(
        '/nologo'
        '/LD'                       # 生成 DLL
        '/EHsc'
        '/O2'
        '/MT'                       # 静态链接 CRT：profiler 在 CLR 极早期加载，
                                    # 动态 CRT 依赖会带来不必要的加载失败风险
        '/std:c++17'
        '/W3'
        '/utf-8'                    # 源文件是 UTF-8；不加则按系统代码页(936)解析，
                                    # 中文注释会引发 C4819 / C2001
        '/D_CRT_SECURE_NO_WARNINGS'
        "/I`"$Here\include`""       # Windows SDK 10.0.26100 已不含 cor.h / corprof.h /
                                    # corhdr.h（微软移出 SDK），取自 dotnet/runtime release/9.0
        'Sts2Profiler.cpp'
        "/Fe:`"$OutDir\Sts2Profiler.dll`""
        # 必须指定完整 obj 文件名，不能用目录形式 "$OutDir\"：
        # 结尾反斜杠紧邻引号时会被 Windows 命令行解析为转义引号(\")，
        # 导致后续所有参数被吞进字符串。
        "/Fo:`"$OutDir\Sts2Profiler.obj`""
        '/link'
        '/DEF:Sts2Profiler.def'
        '/MACHINE:X64'
    )

    & $cl.Source @clArgs
    if ($LASTEXITCODE -ne 0) { throw "编译失败 (exit $LASTEXITCODE)" }
}
finally { Pop-Location }

$dll = Join-Path $OutDir 'Sts2Profiler.dll'
if (-not (Test-Path $dll)) { throw "编译似乎成功但产物不存在: $dll" }

# --- 校验位数 -------------------------------------------------------------
# 位数错误是本方案最隐蔽的失败模式（CLR 静默忽略），务必在此确认。
$fs = [System.IO.File]::OpenRead($dll)
try {
    $br = New-Object System.IO.BinaryReader($fs)
    $fs.Position = 0x3C
    $peOff = $br.ReadInt32()
    $fs.Position = $peOff + 4
    $machine = $br.ReadUInt16()
} finally { $fs.Dispose() }

$arch = switch ($machine) { 0x8664 { 'x64' } 0x14c { 'x86' } default { "未知(0x{0:X})" -f $machine } }

Write-Host ""
Write-Host "[成功] $dll"
Write-Host "[架构] $arch $(if ($arch -ne 'x64') { '  <-- 错误！必须是 x64' })"
