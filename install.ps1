# crewchat installer for Windows (PowerShell 5.1 or later).
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/deepankar17/crewchat/main/install.ps1 | iex"
#
# Installs uv (Astral's Python installer) if it is missing, then crewchat with cloud sync, in its
# own environment with its own Python. Nothing needs administrator rights. Run it again to upgrade.
#
# Settings, as environment variables:
#   $env:CREWCHAT_VERSION = "0.9.3"   install this release ("main" for the latest code)
#   $env:CREWCHAT_LEAN = "1"          leave out cloud sync (about 70 MB of libraries)
#   $env:CREWCHAT_SOURCE = "PATH"     install from a local checkout (for testing this script)
# Native programs report failure through $LASTEXITCODE, checked after each one. ("Stop" would
# also turn uv's progress output into errors in Windows PowerShell 5.1.)
$ErrorActionPreference = "Continue"

$Version = if ($env:CREWCHAT_VERSION) { $env:CREWCHAT_VERSION } else { "0.9.3" }
$Repo = "https://github.com/deepankar17/crewchat"

if ($env:CREWCHAT_SOURCE) { $Source = $env:CREWCHAT_SOURCE }
elseif ($Version -eq "main") { $Source = "$Repo/archive/refs/heads/main.zip" }
else { $Source = "$Repo/archive/refs/tags/v$Version.zip" }
$Spec = if ($env:CREWCHAT_LEAN) { "crewchat @ $Source" } else { "crewchat[cloud] @ $Source" }

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

Write-Host "Installing crewchat $Version..."
& $Uv tool install --force --python 3.12 $Spec
if ($LASTEXITCODE -ne 0) { throw "crewchat did not install (uv exit code $LASTEXITCODE)" }
& $Uv tool update-shell *> $null

$Bin = (& $Uv tool dir --bin).Trim()
if (-not $Bin) { $Bin = Join-Path $env:USERPROFILE ".local\bin" }
$Exe = Join-Path $Bin "crewchat.exe"
$Installed = & $Exe --version
if ($LASTEXITCODE -ne 0 -or -not $Installed) { throw "crewchat installed but does not run; try: $Exe --version" }

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
