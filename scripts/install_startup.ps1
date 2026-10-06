# Installs a startup-folder entry that launches the refresh agent (start_agent.py)
# hidden at logon via pythonw. Creates a .vbs launcher to avoid console flashes.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1
#
# Optional parameters:
#   -PythonExe "C:\path\to\pythonw.exe"   (default: pythonw resolved from PATH)
param(
    [string]$PythonExe = "pythonw"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$AgentScript = Join-Path $ProjectRoot "start_agent.py"

if (-not (Test-Path $AgentScript)) {
    Write-Error "start_agent.py not found at $AgentScript"
}

# pythonw.exe must be used (no console window). Resolve absolute path.
$pyw = (Get-Command $PythonExe -ErrorAction SilentlyContinue)
if ($pyw) { $PythonExe = $pyw.Source }
if ($PythonExe -notmatch "pythonw") {
    $guess = Join-Path (Split-Path -Parent $PythonExe) "pythonw.exe"
    if (Test-Path $guess) { $PythonExe = $guess }
}

$StartupDir = [Environment]::GetFolderPath("Startup")
$VbsPath = Join-Path $StartupDir "weread-keepalive-agent.vbs"

$vbs = @"
' weread-keepalive refresh agent (hidden window launcher)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$ProjectRoot"
sh.Run """$PythonExe"" ""$AgentScript""", 0, False
"@

# VBS files must be ANSI/UTF-16 for wscript; ASCII-only content is safe.
$vbs | Out-File -FilePath $VbsPath -Encoding Ascii

Write-Host "Startup launcher created: $VbsPath"
Write-Host "  interpreter: $PythonExe"
Write-Host "  script     : $AgentScript"
Write-Host "It will start at next logon. To start now:"
Write-Host "  wscript.exe `"$VbsPath`""
