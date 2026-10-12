# Install the AICheckinDaily scheduled task: run `pythonw checkin.py all` daily,
# configured as "Run whether user is logged on or not" WITH a stored password.
#
# Why a stored password (instead of the old /IT interactive-only):
#   Every provider decrypts its local login state via DPAPI (CryptUnprotectData,
#   user master key). A task set to "run whether logged on" AND given the user's
#   password performs a batch logon that loads the user profile, so DPAPI still
#   works with nobody signed in. The passwordless / S4U / service-account variant
#   has no cleartext credential and DOES fail CryptUnprotectData.
#   Re-run this after changing your Windows password to refresh the stored
#   credential. The password is held by the OS (LSA); it is never written here.
#
# ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 as the ANSI/GBK code
# page unless the file has a UTF-8 BOM, so Chinese text here would mis-parse.
#Requires -Version 5
[CmdletBinding()]
param(
    [string]$Time = '10:05',
    [string]$TaskName = 'AICheckinDaily'
)
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Windowless interpreter: local venv pythonw first, else system pythonw.
$pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = 'pythonw.exe' }

$checkin = Join-Path $root 'checkin.py'
$action  = New-ScheduledTaskAction -Execute $pyw -Argument ('"' + $checkin + '" all') -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
# StartWhenAvailable: if a scheduled time was missed (PC off/asleep), run ASAP.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

Write-Host ("Task '" + $TaskName + "': daily at " + $Time + " , run WHETHER OR NOT logged on (stored password; DPAPI still works).")
Write-Host 'Enter your Windows account password so DPAPI can decrypt in a non-interactive session.'
Write-Host ''

$cred = $host.ui.PromptForCredential(
    'AI daily check-in - task credential',
    'Windows account password for the scheduled task (required for DPAPI):',
    ($env:USERDOMAIN + '\' + $env:USERNAME), '')
if (-not $cred) { Write-Warning 'Cancelled. Task not registered.'; exit 1 }

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -User $cred.Target -Password $cred.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null

Write-Host ''
Write-Host 'Registered. Verify the logon mode (should NOT be an interactive-only / S4U task):'
Write-Host ('  schtasks /Query /TN "' + $TaskName + '" /FO LIST /V')
Write-Host ('Run it now and then open checkin.log:  schtasks /Run /TN "' + $TaskName + '"')
Write-Host 'Remove later:  uninstall-schedule.bat'
