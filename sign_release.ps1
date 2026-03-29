param(
    [string]$CertificatePath = '',
    [string]$CertificatePassword = '',
    [string]$TimestampUrl = 'http://timestamp.digicert.com',
    [string]$DigestAlgorithm = 'sha256',
    [switch]$UseMachineStore,
    [string]$Thumbprint = ''
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$targets = @(
    (Join-Path $scriptDir 'dist\udm-wan-speed-monitor-1.0.exe'),
    (Join-Path $scriptDir 'dist\UDM-WAN-Speed-Monitor-1.0.msi')
)

$signtoolCandidates = @()
$command = Get-Command signtool.exe -ErrorAction SilentlyContinue
if ($command -and $command.Source) {
    $signtoolCandidates += [string]$command.Source
}
$signtoolCandidates += @(
    'C:\Program Files (x86)\Windows Kits\10\bin\x64\signtool.exe',
    'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe',
    'C:\Program Files (x86)\Windows Kits\10\bin\10.0.18362.0\x64\signtool.exe',
    'C:\Program Files (x86)\Windows Kits\10\App Certification Kit\signtool.exe'
)
$signtoolCandidates = @($signtoolCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique)

if (-not $signtoolCandidates -or $signtoolCandidates.Count -eq 0) {
    throw 'signtool.exe wurde nicht gefunden. Installiere bitte das Windows SDK oder App Certification Kit.'
}

$signtool = [string]$signtoolCandidates[0]

foreach ($target in $targets) {
    if (-not (Test-Path $target)) {
        throw "Datei nicht gefunden: $target"
    }

    $args = @('sign', '/fd', $DigestAlgorithm, '/td', $DigestAlgorithm, '/tr', $TimestampUrl)

    if ($CertificatePath) {
        $args += @('/f', $CertificatePath)
        if ($CertificatePassword) {
            $args += @('/p', $CertificatePassword)
        }
    }
    elseif ($Thumbprint) {
        $args += @('/sha1', $Thumbprint)
        if ($UseMachineStore) {
            $args += '/sm'
        }
    }
    else {
        $args += '/a'
    }

    $args += $target
    & $signtool @args
    if ($LASTEXITCODE -ne 0) {
        throw "Signieren fehlgeschlagen: $target"
    }

    & $signtool verify /pa $target
    if ($LASTEXITCODE -ne 0) {
        throw "Signaturpruefung fehlgeschlagen: $target"
    }
}
