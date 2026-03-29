$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\UDM WAN Speed Monitor'
$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item (Join-Path $scriptDir 'udm-wan-speed-monitor-1.0.exe') $installDir -Force
Copy-Item (Join-Path $scriptDir 'udm-wan-speed-monitor-v2.ico') $installDir -Force
$target = Join-Path $installDir 'udm-wan-speed-monitor-1.0.exe'
$icon = Join-Path $installDir 'udm-wan-speed-monitor-v2.ico'
$WshShell = New-Object -ComObject WScript.Shell
$desktopShortcut = $WshShell.CreateShortcut((Join-Path $desktop 'UDM WAN Speed Monitor.lnk'))
$desktopShortcut.TargetPath = $target
$desktopShortcut.WorkingDirectory = $installDir
$desktopShortcut.IconLocation = $icon
$desktopShortcut.Save()
$menuShortcut = $WshShell.CreateShortcut((Join-Path $startMenu 'UDM WAN Speed Monitor.lnk'))
$menuShortcut.TargetPath = $target
$menuShortcut.WorkingDirectory = $installDir
$menuShortcut.IconLocation = $icon
$menuShortcut.Save()
Start-Process $target
