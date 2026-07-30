<#
.SYNOPSIS
    带 CoreCLR Profiler 启动《杀戮尖塔 2》。

.DESCRIPTION
    通过三个环境变量启用 profiler，不改动游戏目录任何文件。

    两种模式：

    -Direct   直接启动 SlayTheSpire2.exe。
              游戏会因 "Steamworks initialization failed! No appID found"
              在数次重试后退出 —— 但这不影响验证：profiler 的 Initialize
              在 CLR 启动时即被调用，远早于 Steamworks 初始化。
              适合快速验证 profiler 是否加载。

    (默认)    经 Steam 启动（appid 2868840）。
              Steam 另起进程，不继承本脚本的环境变量，因此必须先在
              Steam 里配置启动选项（脚本会打印出该配置串）。
              适合正式游玩。

.EXAMPLE
    .\launch-with-profiler.ps1 -Direct
#>
param(
    [switch]$Direct,
    [switch]$ClearLog
)

$ErrorActionPreference = 'Stop'

$Root       = Split-Path $PSScriptRoot -Parent
$ProfilerDll = Join-Path $Root 'bin\Sts2Profiler.dll'
$LogPath     = Join-Path $Root 'logs\profiler.log'
$Clsid       = '{27585C9F-BB81-4251-B62F-1B463AB4D58A}'
$GameExe     = 'D:\SteamLibrary\steamapps\common\Slay the Spire 2\SlayTheSpire2.exe'
$AppId       = '2868840'

if (-not (Test-Path $ProfilerDll)) {
    Write-Error "profiler 未编译: $ProfilerDll`n请先运行 src\Sts2Profiler\build.bat"
}

if ($ClearLog -and (Test-Path $LogPath)) { Remove-Item $LogPath -Force }
New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null

Write-Host "Profiler : $ProfilerDll"
Write-Host "CLSID    : $Clsid"
Write-Host "日志     : $LogPath"
Write-Host ""

if ($Direct) {
    # 位数必须匹配：游戏是 64 位进程。PATH_64 与 PATH 都设上以求稳妥。
    $env:CORECLR_ENABLE_PROFILING = '1'
    $env:CORECLR_PROFILER         = $Clsid
    $env:CORECLR_PROFILER_PATH_64 = $ProfilerDll
    $env:CORECLR_PROFILER_PATH    = $ProfilerDll

    Write-Host "直接启动游戏（预期会因缺少 Steam appID 而退出，不影响验证）..." -ForegroundColor Yellow
    Start-Process -FilePath $GameExe -WorkingDirectory (Split-Path $GameExe)
}
else {
    Write-Host "经 Steam 启动前，需在 Steam 中设置启动选项：" -ForegroundColor Cyan
    Write-Host "  右键《Slay the Spire 2》→ 属性 → 启动选项，粘贴：" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "cmd /C `"set CORECLR_ENABLE_PROFILING=1 && set CORECLR_PROFILER=$Clsid && set CORECLR_PROFILER_PATH_64=$ProfilerDll && %command%`"" -ForegroundColor Green
    Write-Host ""
    Start-Process "steam://rungameid/$AppId"
}
