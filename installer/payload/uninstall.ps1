$ErrorActionPreference = 'SilentlyContinue'
$appName = 'UDM WAN Speed Monitor'
$companyName = 'Maxxter'
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\UDM WAN Speed Monitor'
$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$startMenuFolder = Join-Path $startMenu $companyName
$desktopShortcutPath = Join-Path $desktop "$appName.lnk"
$menuShortcutPath = Join-Path $startMenuFolder "$appName.lnk"
$autostartBat = Join-Path ([Environment]::GetFolderPath('Startup')) 'UDM WAN Speed Monitor.bat'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\UDM WAN Speed Monitor'

Get-Process | Where-Object { $_.Path -like (Join-Path $installDir '*') } | Stop-Process -Force
Remove-Item $desktopShortcutPath -Force
Remove-Item $menuShortcutPath -Force
Remove-Item $autostartBat -Force
Remove-Item $uninstallKey -Recurse -Force
Remove-Item $installDir -Recurse -Force
if (Test-Path $startMenuFolder) {
    if (-not (Get-ChildItem $startMenuFolder -Force | Select-Object -First 1)) {
        Remove-Item $startMenuFolder -Force
    }
}
