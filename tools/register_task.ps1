<#
.SYNOPSIS
    为 qqdigest 注册 / 卸载 Windows 计划任务。

.DESCRIPTION
    以指定间隔调用 `python run.py once`，实现「不常驻也能定时」。

    与常驻模式（python run.py run）的区别：
      - 常驻模式：时间精度最高（秒级），但需要进程一直挂着；
      - 计划任务：精度约等于本脚本的 -IntervalMinutes，但进程用完即退。

    两者选一即可，不要同时使用（会重复处理，虽然 slot 幂等不会重复发送）。

.PARAMETER IntervalMinutes
    任务重复间隔，默认 1 分钟。间隔越大，整点送达的偏差越大。

.PARAMETER PythonPath
    Python 解释器路径。默认自动探测。

.PARAMETER Remove
    卸载计划任务并退出。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\register_task.ps1
    powershell -ExecutionPolicy Bypass -File tools\register_task.ps1 -IntervalMinutes 1
    powershell -ExecutionPolicy Bypass -File tools\register_task.ps1 -Remove
#>

[CmdletBinding()]
param(
    [int]$IntervalMinutes = 1,
    [string]$PythonPath = '',
    [string]$TaskName = 'qqdigest-once',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Entry = Join-Path $ProjectRoot 'run.py'
$Config = Join-Path $ProjectRoot 'config.toml'

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Warn2($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }

# --------------------------------------------------------------------------
# 卸载
# --------------------------------------------------------------------------
if ($Remove) {
    Write-Step "卸载计划任务 $TaskName"
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Warn2 "未找到任务 $TaskName，无需卸载"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已卸载。" -ForegroundColor Green
    exit 0
}

# --------------------------------------------------------------------------
# 前置检查
# --------------------------------------------------------------------------
Write-Step "项目根目录：$ProjectRoot"

if (-not (Test-Path $Entry)) { throw "找不到入口文件：$Entry" }
if (-not (Test-Path $Config)) { throw "找不到配置文件：$Config" }

if (-not $PythonPath) {
    $candidates = @(
        'D:\Tools\Python311\python.exe',
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $PythonPath = $c; break }
    }
    if (-not $PythonPath) {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd) { $PythonPath = $cmd.Source }
    }
}
if (-not $PythonPath -or -not (Test-Path $PythonPath)) {
    throw "未找到 Python 解释器，请用 -PythonPath 显式指定"
}
Write-Step "Python 解释器：$PythonPath"

# dry_run 提醒
$raw = Get-Content $Config -Raw -Encoding UTF8
if ($raw -match '(?m)^\s*dry_run\s*=\s*true') {
    Write-Warn2 "config.toml 中 dry_run = true —— 任务只会生成简报，不会向 QQ 发送。"
}

$busy = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($busy) {
    Write-Warn2 "已存在同名任务，将先卸载再重建。"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# --------------------------------------------------------------------------
# 注册
# --------------------------------------------------------------------------
Write-Step "注册计划任务 $TaskName（每 $IntervalMinutes 分钟执行一次 once）"

$action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$Entry`" once" `
    -WorkingDirectory $ProjectRoot

# 从 1 分钟后开始，无限期重复
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration ([TimeSpan]::MaxValue)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'qqdigest：定时检查并发送 QQ 消息简报（调用 run.py once）' | Out-Null

Write-Host ""
Write-Host "注册完成。" -ForegroundColor Green
Write-Host "  任务名   : $TaskName"
Write-Host "  间隔     : 每 $IntervalMinutes 分钟"
Write-Host "  命令     : `"$PythonPath`" `"$Entry`" once"
Write-Host "  工作目录 : $ProjectRoot"
Write-Host ""
Write-Host "常用操作："
Write-Host "  立即执行一次 : Start-ScheduledTask -TaskName $TaskName"
Write-Host "  查看状态     : Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "  查看执行结果 : Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object LastRunTime,LastTaskResult"
Write-Host "  卸载         : powershell -ExecutionPolicy Bypass -File tools\register_task.ps1 -Remove"
Write-Host ""
Write-Warn2 "请确认没有同时以常驻模式（python run.py run）运行，两者并存会重复处理。"
