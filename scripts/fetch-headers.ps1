#Requires -Version 5.1
<#
.SYNOPSIS
    下载 CoreCLR profiling 头文件到 src/Sts2Profiler/include/。

.DESCRIPTION
    Windows SDK 10.0.26100 起已不再包含 cor.h / corprof.h / corhdr.h /
    corerror.h（微软将其移出 SDK），这些头文件现在只存在于 CoreCLR 源码树中。

    注意两组文件位于不同目录，容易搞错：
      src/coreclr/inc/               -> cor.h, corhdr.h
      src/coreclr/pal/prebuilt/inc/  -> corprof.h, corerror.h

    这些头文件属 dotnet/runtime，MIT 许可。因体积较大（corprof.h 约 1.1 MB）
    且属第三方代码，不纳入本仓库版本控制，改由本脚本按需获取。
#>
[CmdletBinding()]
param(
    # 与游戏的 .NET 版本对齐（sts2.runtimeconfig.json 中 tfm = net9.0）
    [string]$Branch = 'release/9.0'
)

$ErrorActionPreference = 'Stop'

$OutDir = Join-Path (Split-Path $PSScriptRoot -Parent) 'src\Sts2Profiler\include'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$base = "https://raw.githubusercontent.com/dotnet/runtime/$Branch/src/coreclr"

$files = @(
    @{ Name = 'cor.h';       Url = "$base/inc/cor.h" }
    @{ Name = 'corhdr.h';    Url = "$base/inc/corhdr.h" }
    @{ Name = 'corprof.h';   Url = "$base/pal/prebuilt/inc/corprof.h" }
    @{ Name = 'corerror.h';  Url = "$base/pal/prebuilt/inc/corerror.h" }
)

Write-Host "分支: $Branch"
Write-Host "目标: $OutDir"
Write-Host ""

$failed = @()
foreach ($f in $files) {
    $out = Join-Path $OutDir $f.Name
    try {
        Invoke-WebRequest -Uri $f.Url -OutFile $out -UseBasicParsing -TimeoutSec 60
        $kb = [math]::Round((Get-Item $out).Length / 1KB, 1)
        Write-Host ("  [OK]   {0,-12} {1,8} KB" -f $f.Name, $kb)
    }
    catch {
        Write-Host ("  [失败] {0,-12} {1}" -f $f.Name, $_.Exception.Message) -ForegroundColor Red
        $failed += $f.Name
    }
}

Write-Host ""
if ($failed.Count -gt 0) {
    Write-Error "以下头文件获取失败: $($failed -join ', ')"
}
Write-Host "完成。接下来运行 src\Sts2Profiler\build.ps1 编译。" -ForegroundColor Green
