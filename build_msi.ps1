$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$installedWix = 'C:\Program Files (x86)\WiX Toolset v3.14\bin'
$portableWix = Join-Path $scriptDir '.tools\wix314'
$wixBin = if (Test-Path (Join-Path $installedWix 'candle.exe')) { $installedWix } else { $portableWix }
$candle = Join-Path $wixBin 'candle.exe'
$light = Join-Path $wixBin 'light.exe'
$obj = Join-Path $scriptDir 'installer\udm-wan-speed-monitor.wixobj'
$wxs = Join-Path $scriptDir 'installer\udm-wan-speed-monitor.wxs'
$out = Join-Path $scriptDir 'dist\UDM-WAN-Speed-Monitor-1.0.1.msi'

if (-not (Test-Path $candle) -or -not (Test-Path $light)) {
    throw 'WiX Toolset 3.14 was not found. Install it or extract the portable binaries to .tools\wix314.'
}

& $candle -nologo -out $obj $wxs
if ($LASTEXITCODE -ne 0) {
    throw "WiX candle.exe failed with exit code $LASTEXITCODE."
}

& $light -nologo -sval -ext WixUIExtension -cultures:en-us -out $out $obj
if ($LASTEXITCODE -ne 0) {
    throw "WiX light.exe failed with exit code $LASTEXITCODE."
}
