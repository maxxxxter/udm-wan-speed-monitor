$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$payloadDir = Join-Path $scriptDir 'installer\payload'
$distDir = Join-Path $scriptDir 'dist'
$targetInstaller = Join-Path $distDir 'UDM-WAN-Speed-Monitor-Installer.exe'
$sedPath = Join-Path $scriptDir 'installer\udm-wan-speed-monitor.sed'

New-Item -ItemType Directory -Force -Path $payloadDir | Out-Null
Copy-Item (Join-Path $distDir 'udm-wan-speed-monitor-1.0.exe') $payloadDir -Force
Copy-Item (Join-Path $distDir 'udm-wan-speed-monitor-v2.ico') $payloadDir -Force
Copy-Item (Join-Path $scriptDir 'install.ps1') $payloadDir -Force
Copy-Item (Join-Path $scriptDir 'uninstall.ps1') $payloadDir -Force
Copy-Item (Join-Path $scriptDir 'installer\install.cmd') $payloadDir -Force

$sed = @"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=0
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=
DisplayLicense=
FinishMessage=
TargetName=$targetInstaller
FriendlyName=UDM WAN Speed Monitor Installer
AppLaunched=install.cmd
PostInstallCmd=<None>
AdminQuietInstCmd=install.cmd
UserQuietInstCmd=install.cmd
SourceFiles=SourceFiles
[Strings]
FILE0=udm-wan-speed-monitor-1.0.exe
FILE1=udm-wan-speed-monitor-v2.ico
FILE2=install.ps1
FILE3=uninstall.ps1
FILE4=install.cmd
[SourceFiles]
SourceFiles0=$payloadDir
[SourceFiles0]
%FILE0%=
%FILE1%=
%FILE2%=
%FILE3%=
%FILE4%=
"@
Set-Content -Path $sedPath -Value $sed -Encoding ASCII
& iexpress.exe /N $sedPath
