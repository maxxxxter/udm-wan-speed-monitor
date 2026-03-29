$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir
pyinstaller --clean 'udm-wan-speed-monitor-release.spec'
