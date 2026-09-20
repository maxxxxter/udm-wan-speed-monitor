$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir
$localPython = Join-Path $scriptDir '.venv\Scripts\python.exe'
$python = if (Test-Path $localPython) { $localPython } else { 'python' }

& $python -m PyInstaller `
    --clean `
    --noconfirm `
    --onefile `
    --windowed `
    --name 'udm-wan-speed-monitor-1.0.1' `
    --icon 'udm-wan-speed-monitor-v2.ico' `
    --version-file 'version_info.txt' `
    'app.py'

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}
