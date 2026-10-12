# 注册 Windows 计划任务：每天定时跑 `pythonw checkin.py all`，且『不管用户是否登录都要运行』。
#
# 为什么要用『带保存密码』的方式而不是老的 /IT（仅登录时运行）：
#   本项目所有登录态解密都依赖 DPAPI（CryptUnprotectData，用户主密钥）。
#   - 『仅登录时运行』(/IT)：用户没登录到桌面就不触发 -> 漏签。
#   - 『不管是否登录都要运行』且【保存了密码】(本脚本)：Task Scheduler 用该凭据做一次
#     Batch 登录并加载用户配置文件 -> DPAPI 主密钥可解 -> 各家签到照常工作。
#   - 『不管是否登录都要运行』但【不保存密码 / S4U / 服务账户】：无明文凭据 ->
#     CryptUnprotectData 失败 -> 全部失败。这就是社区常说“非交互会挂”的真正原因，
#     并非 DPAPI 本身不能非交互，而是“没给它能解锁主密钥的凭据”。
#
# 注意：
#   * 改动 Windows 登录密码后，保存的任务凭据失效，需重新运行本脚本刷新。
#   * 密码由操作系统(LSA)保管，不写入本仓库任何文件。
#   * 不想保存密码就退回交互模式：见 README「仅登录时运行」备选命令。
#Requires -Version 5
[CmdletBinding()]
param(
    [string]$Time = '10:05',
    [string]$TaskName = 'AICheckinDaily'
)
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# 无窗口解释器：优先本地 venv 的 pythonw，其次系统 pythonw
$pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = 'pythonw.exe' }

$action  = New-ScheduledTaskAction -Execute $pyw -Argument "`"$root\checkin.py`" all" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
# StartWhenAvailable：错过时间点（关机/睡眠）后开机尽快补跑；电池下也允许
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

Write-Host "计划任务 '$TaskName'：每天 $Time 运行，『不管用户是否登录都要运行』。"
Write-Host '需要当前用户的密码以让 DPAPI 在非交互会话可用（密码仅由系统保管，不落盘到项目）。'
Write-Host ''

$cred = $host.ui.PromptForCredential('AI 每日签到 - 计划任务凭据',
    '输入用于『不管是否登录都运行』的 Windows 账户密码（DPAPI 解密需要）：',
    "$env:USERDOMAIN\$env:USERNAME", '')
if (-not $cred) { Write-Warning '已取消，未注册任务。'; exit 1 }

$null = Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -User $cred.Target -Password $cred.GetNetworkCredential().Password -RunLevel Limited -Force

Write-Host ''
Write-Host "已注册。校验： schtasks /Query /TN `"$TaskName`" /FO LIST /V   （'作为用户运行'/'登录模式'应为 Password/交互式之外的凭据登录）"
Write-Host "立即试跑： schtasks /Run /TN `"$TaskName`"  然后查看 checkin.log"
Write-Host "卸载：     uninstall-schedule.bat"
