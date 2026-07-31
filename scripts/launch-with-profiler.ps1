<#
.SYNOPSIS
    带 CoreCLR Profiler 启动《杀戮尖塔 2》。

.DESCRIPTION
    通过环境变量启用 profiler，不改动游戏目录任何文件。

    -Direct   直接启动 SlayTheSpire2.exe。
              游戏会因 "Steamworks initialization failed! No appID found"
              在数次重试后退出 —— 但不影响验证 profiler 是否加载：
              profiler 的 Initialize 在 CLR 启动时即被调用，远早于
              Steamworks 初始化。适合快速验证注入链路。

    (默认)    经 Steam 启动（appid 2868840）。
              Steam 另起进程、不继承本脚本的环境变量，因此必须先在 Steam
              中配置启动选项（脚本会打印出该配置串）。适合正式游玩。

.EXAMPLE
    .\launch-with-profiler.ps1 -Direct -ClearLog
#>
[CmdletBinding()]
param(
    [switch]$Direct,
    [switch]$ClearLog,
    # 自动探测失败时可显式指定游戏 exe
    [string]$GameExe,
    [int]$Port,
    # 帧循环接入默认开启（阶段 3 的硬前置）；怀疑它导致问题时用本开关排除
    [switch]$NoAttachFrame
)

$ErrorActionPreference = 'Stop'

$Root        = Split-Path $PSScriptRoot -Parent
$ProfilerDll = Join-Path $Root 'bin\Sts2Profiler.dll'
$LogPath     = Join-Path $Root 'logs\profiler.log'
$Clsid       = '{27585C9F-BB81-4251-B62F-1B463AB4D58A}'
$AppId       = '2868840'

# ---------------------------------------------------------------------------
#  自动探测游戏安装位置
#
#  早期版本把本机路径写死在脚本里，换机器即失效且报错含糊。改为按 Steam 的
#  标准布局探测：注册表取 Steam 根目录 -> libraryfolders.vdf 枚举全部库
#  -> 逐个尝试。用户仍可用 -GameExe 覆盖。
# ---------------------------------------------------------------------------
function Find-GameExe {
    param([string]$Override)

    if ($Override) {
        if (Test-Path $Override) { return (Resolve-Path $Override).Path }
        throw "指定的游戏 exe 不存在: $Override"
    }

    $steam = $null
    foreach ($key in @('HKCU:\Software\Valve\Steam', 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam')) {
        try {
            $v = (Get-ItemProperty -Path $key -ErrorAction Stop).SteamPath
            if ($v) { $steam = $v.Replace('/', '\'); break }
        } catch { }
    }
    if (-not $steam) { $steam = 'C:\Program Files (x86)\Steam' }

    # Steam 根目录本身也是一个库
    $libs = New-Object System.Collections.Generic.List[string]
    $libs.Add($steam)

    $vdf = Join-Path $steam 'steamapps\libraryfolders.vdf'
    if (Test-Path $vdf) {
        foreach ($line in Get-Content $vdf) {
            if ($line -match '"path"\s+"(.+?)"') {
                $libs.Add($matches[1].Replace('\\', '\'))
            }
        }
    }

    foreach ($lib in $libs) {
        $exe = Join-Path $lib 'steamapps\common\Slay the Spire 2\SlayTheSpire2.exe'
        if (Test-Path $exe) { return $exe }
    }

    throw "未能自动探测到游戏。已尝试的 Steam 库：`n  $($libs -join "`n  ")`n请用 -GameExe 显式指定。"
}

if (-not (Test-Path $ProfilerDll)) {
    throw "profiler 未编译: $ProfilerDll`n请先运行 src\Sts2Profiler\build.ps1"
}

if ($ClearLog -and (Test-Path $LogPath)) { Remove-Item $LogPath -Force }
New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null

Write-Host "仓库     : $Root"
Write-Host "Profiler : $ProfilerDll"
Write-Host "CLSID    : $Clsid"

if ($Direct) {
    $exe = Find-GameExe -Override $GameExe
    Write-Host "游戏     : $exe"
    Write-Host ""

    # 位数必须匹配：游戏是 64 位进程。PATH_64 与 PATH 都设以求稳妥。
    $env:CORECLR_ENABLE_PROFILING = '1'
    $env:CORECLR_PROFILER         = $Clsid
    $env:CORECLR_PROFILER_PATH_64 = $ProfilerDll
    $env:CORECLR_PROFILER_PATH    = $ProfilerDll
    # 桥接层从临时副本加载，无法自行反推仓库位置，需由此传入
    $env:STS2MCP_REPO             = $Root
    if ($Port) { $env:STS2MCP_PORT = "$Port" }

    # 与 launch-steam.cmd 保持一致。两个启动器的环境变量必须同步 ——
    # 曾因只在 .cmd 里加了 ATTACH_FRAME 而本脚本没加，导致排查了半天
    # 「代码为何不生效」，实际是走了另一条启动路径。
    # 需要关闭时传 -NoAttachFrame。
    if (-not $NoAttachFrame) { $env:STS2MCP_ATTACH_FRAME = '1' }

    Write-Host "直接启动游戏（预期会因缺少 Steam appID 而退出，不影响验证）..." -ForegroundColor Yellow
    Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe)
}
else {
    $cmd = Join-Path $PSScriptRoot 'launch-steam.cmd'
    Write-Host ""
    Write-Host "经 Steam 启动前，需在 Steam 中设置启动选项：" -ForegroundColor Cyan
    Write-Host "  右键《Slay the Spire 2》→ 属性 → 启动选项，粘贴：" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "`"$cmd`" %command%" -ForegroundColor Green
    Write-Host ""
    Write-Host "（不想启用时把该栏清空即可，游戏恢复原样）" -ForegroundColor DarkGray
    Write-Host ""
    Start-Process "steam://rungameid/$AppId"
}
