# crewchat installer for Windows (PowerShell 5.1 or later).
#
#   powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/deepankar17/crewchat/main/install.ps1 | iex"
#
# Installs uv (Astral's Python installer) if it is missing, then crewchat with cloud sync, in its
# own environment with its own Python. Nothing needs administrator rights. Run it again to upgrade.
#
# Settings, as environment variables:
#   $env:CREWCHAT_VERSION = "0.9.8"   install this release ("main" for the latest code)
#   $env:CREWCHAT_LEAN = "1"          leave out cloud sync (about 70 MB of libraries)
#   $env:CREWCHAT_SOURCE = "PATH"     install from a local checkout (for testing this script)
# Native programs report failure through $LASTEXITCODE, checked after each one. ("Stop" would
# also turn uv's progress output into errors in Windows PowerShell 5.1.)
$ErrorActionPreference = "Continue"

$Version = if ($env:CREWCHAT_VERSION) { $env:CREWCHAT_VERSION } else { "0.9.8" }
$Repo = "https://github.com/deepankar17/crewchat"

$Fallback = $null
if ($env:CREWCHAT_SOURCE) { $Source = $env:CREWCHAT_SOURCE }
elseif ($Version -eq "main") { $Source = "$Repo/archive/refs/heads/main.zip" }
else {
    # The release's own package file: GitHub counts its downloads (nothing about you is sent).
    # Releases before 0.9.7 have none, and install from their source archive instead.
    $Source = "$Repo/releases/download/v$Version/crewchat-$Version-py3-none-any.whl"
    $Fallback = "$Repo/archive/refs/tags/v$Version.zip"
}
function Spec([string]$From) { if ($env:CREWCHAT_LEAN) { "crewchat @ $From" } else { "crewchat[cloud] @ $From" } }

$Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $Uv) {
    $Candidate = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path $Candidate) { $Uv = $Candidate }
}
if (-not $Uv) {
    Write-Host "Installing uv, which installs crewchat and the Python it needs..."
    $env:UV_NO_MODIFY_PATH = "1"
    try { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression | Out-Null }
    catch { throw "could not install uv: $_" }
    Remove-Item Env:UV_NO_MODIFY_PATH
    $Uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (-not (Test-Path $Uv)) { throw "uv did not install; see https://docs.astral.sh/uv/getting-started/installation/" }
}

# A running crewchat (the server, or an agent's hook waiting for messages) keeps its program
# files open, and Windows cannot replace open files. Stop them, and start the server again after.
$Running = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -and
    ($_.CommandLine -match '\\uv\\tools\\crewchat\\' -or $_.CommandLine -match 'crewchat(\.exe)?"?\s+(serve|hook)\b')
})
$Restart = $false
if ($Running.Count -gt 0) {
    Write-Host "Stopping the running crewchat for the upgrade (agents waiting for messages pick up again on their next turn)..."
    $Restart = [bool]($Running | Where-Object { $_.CommandLine -match '\bserve\b' })
    schtasks /End /TN crewchat *> $null
    $Running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

Write-Host "Installing crewchat $Version..."
& $Uv tool install --force --python 3.12 (Spec $Source)
if ($LASTEXITCODE -ne 0 -and $Fallback) {
    Write-Host "Release $Version has no package file; installing it from its source instead..."
    & $Uv tool install --force --python 3.12 (Spec $Fallback)
}
if ($LASTEXITCODE -ne 0) { throw "crewchat did not install (uv exit code $LASTEXITCODE)" }
& $Uv tool update-shell *> $null

$Bin = (& $Uv tool dir --bin).Trim()
if (-not $Bin) { $Bin = Join-Path $env:USERPROFILE ".local\bin" }
$Exe = Join-Path $Bin "crewchat.exe"
$Installed = & $Exe --version
if ($LASTEXITCODE -ne 0 -or -not $Installed) { throw "crewchat installed but does not run; try: $Exe --version" }
if ($Restart) {
    # The new crewchat starts its server the way it starts at log on: Task Scheduler, or the
    # user's startup programs when Task Scheduler needed an administrator.
    & $Exe restart *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Started the crewchat server again."
    } else {
        Write-Host "The crewchat server was stopped for the upgrade: start it again with crewchat start."
    }
}

# The logo (a speech bubble holding three connected agents), on a console that can show it. It is
# built from character codes so this file stays plain ASCII, which Windows PowerShell reads right
# however it is started.
function Show-Logo([string]$Ver) {
    if ([Console]::IsOutputRedirected) { return }
    $plain = [bool]$env:NO_COLOR
    function Part([string]$Text, [string]$Colour) {
        if ($plain -or -not $Colour) { Write-Host $Text -NoNewline } else { Write-Host $Text -NoNewline -ForegroundColor $Colour }
    }
    $h = [string][char]0x2500; $v = [string][char]0x2502; $dot = [string][char]0x25CF
    Write-Host ""
    Part ("  " + [char]0x256D + ($h * 11) + [char]0x256E) Blue; Write-Host ""
    Part "  $v" Blue; Part "     $dot     " White; Part $v Blue; Part "   crewchat " White; Write-Host $Ver
    Part "  $v" Blue; Part ("    " + [char]0x2571 + " " + [char]0x2572 + "    ") Gray; Part $v Blue; Write-Host "   one group chat for all your AI agents"
    Part "  $v" Blue; Part "   $dot" White; Part ($h * 3) Gray; Part "$dot   " White; Part $v Blue; Write-Host ""
    Part ("  " + [char]0x2570 + ($h * 2) + [char]0x256E + " " + [char]0x256D + ($h * 6) + [char]0x256F) Blue; Write-Host ""
    Part ("     " + $v + [char]0x2571) Blue; Write-Host ""
}

Show-Logo ($Installed -replace '^crewchat ', '')
Write-Host ""
Write-Host "Installed $Installed."
if (-not (($env:Path -split ";") -contains $Bin)) {
    $env:Path = "$Bin;$env:Path"
    Write-Host "Open a new terminal if the crewchat command is not found."
}
Write-Host ""
Write-Host "Start a chat: open a terminal in your project folder and run"
Write-Host ""
Write-Host "  crewchat start"
Write-Host ""
Write-Host "Upgrade later by running this installer again. Remove: crewchat service uninstall; uv tool uninstall crewchat"
