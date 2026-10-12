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
# The password is typed in THIS console (masked), not via a GUI dialog:
#   $Host.UI.PromptForCredential sometimes renders no window at all inside a
#   plain console host, so the old version silently got nothing. Read-Host
#   -AsSecureString always works in an interactive console.
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

$account = ('{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME)

Write-Host ("Task '" + $TaskName + "': daily at " + $Time + " , run WHETHER OR NOT logged on (stored password; DPAPI still works).")
Write-Host ("Account: " + $account)
Write-Host 'Type your Windows account password below (input is hidden). Needed so DPAPI can decrypt in a non-interactive session.'

$secure = Read-Host 'Windows password' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
if ([string]::IsNullOrEmpty($plain)) { Write-Warning 'Empty password. Task not registered.'; exit 1 }

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -User $account -Password $plain -RunLevel Limited -Force | Out-Null
}
catch {
    $plain = $null
    Write-Error ("Registration failed (wrong password or logon-right denied): " + $_.Exception.Message)
    Write-Host 'Tip: your account may need the "Log on as a batch job" right; retry with the correct password.'
    exit 1
}
$plain = $null

$next = (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime
Write-Host ''
Write-Host ("[OK] Registered '" + $TaskName + "' daily at " + $Time + " (run whether logged on, StartWhenAvailable).")
Write-Host ('  next run: ' + $next)
Write-Host ('  verify : schtasks /Query /TN "' + $TaskName + '" /FO LIST /V   (Logon Mode should be "Run whether user is logged on or not")')
Write-Host ('  test   : schtasks /Run /TN "' + $TaskName + '"  then open checkin.log')
Write-Host ('  remove : uninstall-schedule.bat')
