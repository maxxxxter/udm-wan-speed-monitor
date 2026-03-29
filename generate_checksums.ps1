$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$distDir = Join-Path $scriptDir 'dist'
$targets = @(
    'udm-wan-speed-monitor-1.0.exe',
    'UDM-WAN-Speed-Monitor-1.0.msi',
    'UDM-WAN-Speed-Monitor-Installer.exe'
)
$outFile = Join-Path $distDir 'SHA256SUMS.txt'
$lines = foreach ($name in $targets) {
    $path = Join-Path $distDir $name
    if (Test-Path $path) {
        $hash = (Get-FileHash $path -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash *$name"
    }
}
Set-Content -Path $outFile -Value $lines -Encoding ASCII
