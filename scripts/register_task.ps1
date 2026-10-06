# Registers a Windows Scheduled Task that runs the headless cookie refresh
# every 15 minutes.
#
# Usage (elevated PowerShell not required for current-user tasks):
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
#
# Optional parameters:
#   -PythonExe "C:\path\to\python.exe"   (default: python on PATH)
#   -IntervalMinutes 15
param(
    [string]$PythonExe = "python",
    [int]$IntervalMinutes = 15
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ScriptPath = Join-Path $ProjectRoot "refresh_cookie.py"

if (-not (Test-Path $ScriptPath)) {
    Write-Error "refresh_cookie.py not found at $ScriptPath"
}

# Resolve absolute python path if it resolves on PATH
$resolved = (Get-Command $PythonExe -ErrorAction SilentlyContinue)
if ($resolved) { $PythonExe = $resolved.Source }

$TaskName = "WereadKeepalive Refresh"
$Action = New-ScheduledTaskAction -Execute $PythonExe `
    -Argument "`"$ScriptPath`" --headless --cooldown-hours 0" `
    -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

# Remove existing task with the same name, then register
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }

Register-ScheduledTask -TaskName $TaskName -Action $Action `
    -Trigger $Trigger -Settings $Settings -Description `
    "WeRead login-state keepalive: headless refresh every $IntervalMinutes minutes (weread-keepalive)" | Out-Null

Write-Host "Scheduled task '$TaskName' registered."
Write-Host "  action : $PythonExe `"$ScriptPath`" --headless --cooldown-hours 0"
Write-Host "  every  : $IntervalMinutes minutes"
Write-Host "Verify with: Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "Run once now: Start-ScheduledTask -TaskName '$TaskName'"
