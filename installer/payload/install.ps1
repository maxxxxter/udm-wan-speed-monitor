$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$appName = 'UDM WAN Speed Monitor'
$companyName = 'Maxxter'
$version = '1.0.0'
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\UDM WAN Speed Monitor'
$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$startMenuFolder = Join-Path $startMenu $companyName
$target = Join-Path $installDir 'udm-wan-speed-monitor-1.0.exe'
$icon = Join-Path $installDir 'udm-wan-speed-monitor-v2.ico'
$uninstallScript = Join-Path $installDir 'uninstall.ps1'
$desktopShortcutPath = Join-Path $desktop "$appName.lnk"
$menuShortcutPath = Join-Path $startMenuFolder "$appName.lnk"
$uninstallCmd = "powershell.exe -ExecutionPolicy Bypass -File `"$uninstallScript`""

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
New-Item -ItemType Directory -Force -Path $startMenuFolder | Out-Null
Copy-Item (Join-Path $scriptDir 'udm-wan-speed-monitor-1.0.exe') $installDir -Force
Copy-Item (Join-Path $scriptDir 'udm-wan-speed-monitor-v2.ico') $installDir -Force
Copy-Item (Join-Path $scriptDir 'uninstall.ps1') $installDir -Force

$WshShell = New-Object -ComObject WScript.Shell
$desktopShortcut = $WshShell.CreateShortcut($desktopShortcutPath)
$desktopShortcut.TargetPath = $target
$desktopShortcut.WorkingDirectory = $installDir
$desktopShortcut.IconLocation = $icon
$desktopShortcut.Save()

$menuShortcut = $WshShell.CreateShortcut($menuShortcutPath)
$menuShortcut.TargetPath = $target
$menuShortcut.WorkingDirectory = $installDir
$menuShortcut.IconLocation = $icon
$menuShortcut.Save()

$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\UDM WAN Speed Monitor'
New-Item -Path $uninstallKey -Force | Out-Null
Set-ItemProperty -Path $uninstallKey -Name 'DisplayName' -Value $appName
Set-ItemProperty -Path $uninstallKey -Name 'Publisher' -Value $companyName
Set-ItemProperty -Path $uninstallKey -Name 'DisplayVersion' -Value $version
Set-ItemProperty -Path $uninstallKey -Name 'DisplayIcon' -Value $target
Set-ItemProperty -Path $uninstallKey -Name 'InstallLocation' -Value $installDir
Set-ItemProperty -Path $uninstallKey -Name 'UninstallString' -Value $uninstallCmd
Set-ItemProperty -Path $uninstallKey -Name 'QuietUninstallString' -Value $uninstallCmd
Set-ItemProperty -Path $uninstallKey -Name 'NoModify' -Value 1 -Type DWord
Set-ItemProperty -Path $uninstallKey -Name 'NoRepair' -Value 1 -Type DWord

Start-Process $target
