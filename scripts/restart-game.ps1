<#
.SYNOPSIS
    重启《杀戮尖塔 2》并等待桥接层就绪。

.DESCRIPTION
    桥接层由 profiler 在**进程启动那一刻**载入临时副本，重新编译对已在运行的
    游戏毫无影响 —— 每次改完桥接层都必须重启游戏才能验证。本脚本把这套流程
    固化下来，省掉人工来回。

    做的事：结束游戏进程 → 等端口释放 → 经 Steam 重新启动 → 轮询 /health
    直到桥接层接入帧循环 → 打印新旧时间戳供核对。

    ⚠️ 强制结束进程会丢掉未保存的进度：游戏在房间边界存档，重启会回退到
    最近一个存档点。战斗中途重启多半会退回本场战斗开始前。

.PARAMETER SkipKill
    游戏没在运行时用，跳过结束进程直接启动。

.PARAMETER NoResume
    不自动点主菜单的「继续游戏」。默认会点 —— 重启后游戏停在主菜单、
    存档并未载入，不点就没有 RunState，等于只重启了一半。

.PARAMETER TimeoutSec
    等待桥接层就绪的上限，默认 120 秒（游戏启动约 30 秒，桥接层再延迟 6 秒）。

.EXAMPLE
    .\restart-game.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipKill,
    [switch]$NoResume,
    [int]$TimeoutSec = 120
)

$ErrorActionPreference = 'Stop'

$Root    = Split-Path $PSScriptRoot -Parent
$AppId   = '2868840'
$Port    = if ($env:STS2MCP_PORT) { $env:STS2MCP_PORT } else { '8765' }
$Health  = "http://127.0.0.1:$Port/health"
$DllPath = Join-Path $Root 'src\Sts2Bridge\bin\Release\net9.0\Sts2Bridge.dll'

function Wait-Until {
    param([scriptblock]$Condition, [int]$Seconds, [string]$What)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { return $true }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "  [超时] $What" -ForegroundColor Yellow
    return $false
}

if (Test-Path $DllPath) {
    Write-Host "桥接层 dll 编译于 $((Get-Item $DllPath).LastWriteTime.ToString('HH:mm:ss'))"
} else {
    Write-Host "[警告] 桥接层未编译: $DllPath" -ForegroundColor Yellow
}

if (-not $SkipKill) {
    $proc = Get-Process SlayTheSpire2 -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host "结束游戏进程 pid=$($proc.Id)（启动于 $($proc.StartTime.ToString('HH:mm:ss')))..."
        $proc | Stop-Process -Force
        Wait-Until { -not (Get-Process SlayTheSpire2 -ErrorAction SilentlyContinue) } 30 '游戏进程未退出' | Out-Null
    } else {
        Write-Host "游戏未在运行"
    }

    # 承载环境变量的启动器 cmd.exe 正常会随子进程一并退出。若它还活着，
    # 说明这次启动没有重新读取 launch-steam.cmd —— 新增的环境变量不会生效。
    $relay = Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
             Where-Object { $_.CommandLine -like '*launch-steam.cmd*' }
    if ($relay) {
        Write-Host "结束残留的启动器进程 pid=$($relay.ProcessId)"
        foreach ($r in $relay) { Stop-Process -Id $r.ProcessId -Force -ErrorAction SilentlyContinue }
    }

    # 旧进程持有 HTTP 端口，不等它释放就启动会撞端口
    Wait-Until {
        -not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    } 20 "端口 $Port 未释放" | Out-Null
}

Write-Host "经 Steam 启动（appid $AppId）..."
Start-Process "steam://rungameid/$AppId"

Write-Host "等待桥接层就绪（上限 $TimeoutSec 秒）..."
$ready = Wait-Until {
    try {
        $h = Invoke-RestMethod -Uri $Health -TimeoutSec 2
        # attached 才算真就绪：未接入帧循环时动作一律无法下发
        return ($h.ok -and $h.attached)
    } catch { return $false }
} $TimeoutSec '桥接层未就绪'

if ($ready) {
    $h = Invoke-RestMethod -Uri $Health -TimeoutSec 3
    $p = Get-Process -Id $h.pid -ErrorAction SilentlyContinue
    $started = if ($p) { $p.StartTime.ToString('HH:mm:ss') } else { '?' }
    Write-Host ""
    Write-Host "就绪：pid=$($h.pid) 启动于 $started  attached=$($h.attached) frame=$($h.frame)" -ForegroundColor Green
    # 游戏启动时间必须晚于 dll 编译时间，否则加载的还是旧桥接层
    if ((Test-Path $DllPath) -and $p -and $p.StartTime -lt (Get-Item $DllPath).LastWriteTime) {
        Write-Host "[警告] 游戏启动早于 dll 编译时间 —— 加载的可能是旧桥接层" -ForegroundColor Yellow
    }

    if (-not $NoResume) {
        # 桥接层就绪（HTTP 起来了）**早于**主菜单加载完成，实测差几秒。
        # 不等就会看到 can_resume=false 而误判为「没有存档」。
        Wait-Until {
            try {
                $s = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/state" -TimeoutSec 2
                return ($s.in_run -or $s.can_resume)
            } catch { return $false }
        } 60 '主菜单未加载完成' | Out-Null

        $state = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/state" -TimeoutSec 5
        if ($state.in_run) {
            Write-Host "已在局中，无需继续"
        } elseif ($state.can_resume) {
            Write-Host "点主菜单「继续游戏」载入存档..."
            $r = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/action/resume_run?state=0" -TimeoutSec 60
            if ($r.ok) {
                $s2 = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/state" -TimeoutSec 5
                Write-Host ("载入完成：第 {0} 层 {1}  HP {2}/{3}" -f `
                    $s2.run.total_floor, $s2.run.room, $s2.player.hp, $s2.player.max_hp) -ForegroundColor Green
            } else {
                Write-Host "继续失败: $($r.error) $($r.detail)" -ForegroundColor Yellow
            }
        } else {
            Write-Host "主菜单上没有可用的「继续游戏」（没有存档？）" -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "桥接层未在 $TimeoutSec 秒内就绪。检查 logs\bridge.log 与 logs\launcher.log" -ForegroundColor Red
    exit 1
}
