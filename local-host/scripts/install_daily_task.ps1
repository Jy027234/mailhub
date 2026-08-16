# Install a daily 08:00 MailHub digest task (Windows Task Scheduler).
# The task runs b4_daily.py with the local-host environment; logs to
# local-host/logs/daily-task.log.  Rerun to update the schedule.
$ErrorActionPreference = 'Stop'
$localRoot = Split-Path -Parent $PSScriptRoot
$action = New-ScheduledTaskAction `
    -Execute 'python' `
    -Argument "scripts\b4_daily.py" `
    -WorkingDirectory $localRoot
$trigger = New-ScheduledTaskTrigger -Daily -At 8:00am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 50)
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings `
    -Description 'MailHub B4 每日摘要：自主周期 + 候选清单报告'
try {
    Register-ScheduledTask -TaskName 'MailHub-B4-DailyDigest' -InputObject $task -Force | Out-Null
    Write-Host '已注册计划任务 MailHub-B4-DailyDigest（每天 08:00）。'
    Write-Host '立即手动运行一次: Start-ScheduledTask -TaskName MailHub-B4-DailyDigest'
} catch {
    Write-Host "注册失败（可能需要管理员权限）: $($_.Exception.Message)"
    Write-Host '手动替代：任务计划程序 → 创建任务 → 程序 python，参数 scripts\b4_daily.py，起始于本目录。'
}
