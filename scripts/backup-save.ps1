#Requires -Version 5.1
<#
.SYNOPSIS
    备份 / 还原《杀戮尖塔 2》的存档（spec.md 的 0.4）。

.DESCRIPTION
    调试期要频繁杀进程重启游戏，而存档只在特定节点落盘 —— 每次强杀都退回到
    最近一次存档点。跑长局之前先存一份，出事能退回来。

    存档的真实位置（2026-08-01 实测，不是 spec 里原先写的 %APPDATA%\SlayTheSpire2\ 根目录）：

        %APPDATA%\SlayTheSpire2\steam\<steamId>\
            profile.save              账号级
            settings.save             设置
            profile1\saves\
                progress.save         **当前进度，含正在进行的那一局**
                progress.save.backup  游戏自己留的上一版
                prefs.save
                history\*.run         已结束的每一局各一个文件

    根目录下的 default\ 只有 settings.save，且日期停在 6 月 —— 那是没登录
    Steam 时的档，跟着 Steam 跑的这份完全不在那儿。**照 spec 原文备份根目录
    会备份到一份空壳**，这也是这条技术债一直没做对的原因。

    shader_cache / logs / sentry 一律不备份：加起来 12 MB 且随时可重建，
    而真正的存档只有约 300 KB。

.PARAMETER Restore
    还原。传备份目录名，或 latest 表示最近一份。

.PARAMETER List
    列出已有的备份。

.PARAMETER Keep
    保留最近几份，多余的删掉。默认 20。

.PARAMETER Force
    还原时即使游戏正在运行也照做（**不建议**，见下方警告）。

.EXAMPLE
    powershell -File scripts\backup-save.ps1
    powershell -File scripts\backup-save.ps1 -List
    powershell -File scripts\backup-save.ps1 -Restore latest
#>
[CmdletBinding()]
param(
    [string]$Restore,
    [switch]$List,
    [int]$Keep = 20,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$SaveRoot  = Join-Path $env:APPDATA 'SlayTheSpire2\steam'
$BackupDir = Join-Path (Split-Path $PSScriptRoot -Parent) 'backup\saves'

function Get-Backups {
    if (-not (Test-Path $BackupDir)) { return @() }
    Get-ChildItem $BackupDir -Directory | Sort-Object Name
}

function Format-Size ($bytes) { "{0:N0} KB" -f ($bytes / 1KB) }

# --------------------------------------------------------------------------
#  列出
# --------------------------------------------------------------------------
if ($List) {
    $all = Get-Backups
    if (-not $all) { "还没有任何备份（$BackupDir）"; exit 0 }
    foreach ($b in $all) {
        $size = (Get-ChildItem $b.FullName -Recurse -File | Measure-Object Length -Sum).Sum
        $note = Get-Content (Join-Path $b.FullName 'note.txt') -Raw -ErrorAction SilentlyContinue
        # 没有 note.txt 时 $note 是 $null，而 `$null -replace` 返回的是**空数组**
        # 不是空串 —— 直接喂给 -f 会打印出 System.Object[]。套一层 [string] 收口。
        $note = [string]($note -replace '\r?\n', ' ')
        "{0}  {1,10}  {2}" -f $b.Name, (Format-Size $size), $note.Trim()
    }
    exit 0
}

# --------------------------------------------------------------------------
#  还原
# --------------------------------------------------------------------------
if ($Restore) {
    # 游戏运行时还原没有意义：进度在内存里，退出时会照内存重新写一遍，
    # 把刚还原的档直接盖掉。这不是保守，是实打实会白干一次。
    $running = Get-Process -Name 'SlayTheSpire2' -ErrorAction SilentlyContinue
    if ($running -and -not $Force) {
        Write-Error "游戏正在运行（pid=$($running.Id)）。请先退出游戏再还原 —— 否则退出时会用内存里的进度覆盖掉还原结果。确实要强来就加 -Force。"
    }

    $src = if ($Restore -eq 'latest') { (Get-Backups | Select-Object -Last 1) }
           else { Get-Backups | Where-Object Name -eq $Restore }
    if (-not $src) { Write-Error "找不到备份：$Restore（用 -List 看有哪些）" }

    # 还原前先把现状再存一份，否则「还原错了一份」就无处可退
    $safety = Join-Path $BackupDir ("{0}-还原前" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    if (Test-Path $SaveRoot) {
        New-Item -ItemType Directory -Path $safety -Force | Out-Null
        Copy-Item "$SaveRoot\*" $safety -Recurse -Force
        Set-Content -Path (Join-Path $safety 'note.txt') -Encoding UTF8 `
            -Value "还原 $($src.Name) 之前的现状"
    }

    Copy-Item (Join-Path $src.FullName '*') $SaveRoot -Recurse -Force -Exclude 'note.txt'
    "已还原 $($src.Name) → $SaveRoot"
    "还原前的现状存在 $safety"
    ""
    "⚠️ Steam 云同步：游戏下次启动时，云端那份可能覆盖回来。若还原没生效，"
    "   在 Steam 里对本游戏临时关闭「Steam 云同步」再启动。"
    exit 0
}

# --------------------------------------------------------------------------
#  备份
# --------------------------------------------------------------------------
if (-not (Test-Path $SaveRoot)) {
    Write-Error "找不到存档目录：$SaveRoot（游戏至少要用 Steam 账号跑过一次）"
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$dest  = Join-Path $BackupDir $stamp
New-Item -ItemType Directory -Path $dest -Force | Out-Null
Copy-Item "$SaveRoot\*" $dest -Recurse -Force

# 备份里塞一句话说明「这是哪一局」—— 光看时间戳，一周后自己也认不出来。
# progress.save 是二进制/加密的，读不出层数，所以只记文件时间与体积。
$progress = Get-ChildItem $dest -Recurse -Filter 'progress.save' | Select-Object -First 1
$note = if ($progress) {
    "progress.save {0:yyyy-MM-dd HH:mm:ss} {1:N0} 字节" -f $progress.LastWriteTime, $progress.Length
} else { "未找到 progress.save" }
Set-Content -Path (Join-Path $dest 'note.txt') -Value $note -Encoding UTF8

$size = (Get-ChildItem $dest -Recurse -File | Measure-Object Length -Sum).Sum
"已备份 → $dest  ($(Format-Size $size))"
$note

# 只留最近 $Keep 份
$all = Get-Backups
if ($all.Count -gt $Keep) {
    $all | Select-Object -First ($all.Count - $Keep) | ForEach-Object {
        Remove-Item $_.FullName -Recurse -Force
        "已清理旧备份 $($_.Name)"
    }
}


