$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$wixBin = 'C:\Program Files (x86)\WiX Toolset v3.14\bin'
$candle = Join-Path $wixBin 'candle.exe'
$light = Join-Path $wixBin 'light.exe'
$obj = Join-Path $scriptDir 'installer\udm-wan-speed-monitor.wixobj'
$wxs = Join-Path $scriptDir 'installer\udm-wan-speed-monitor.wxs'
$out = Join-Path $scriptDir 'dist\UDM-WAN-Speed-Monitor-1.0.msi'

& $candle -nologo -out $obj $wxs
& $light -nologo -ext WixUIExtension -cultures:en-us -out $out $obj
