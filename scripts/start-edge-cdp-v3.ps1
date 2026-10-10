$ErrorActionPreference = 'Stop'

$appPathKeys = @(
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe',
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe',
    'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe'
)

$edgeExecutable = $null
foreach ($keyPath in $appPathKeys) {
    if (Test-Path -LiteralPath $keyPath) {
        $registeredPath = (Get-Item -LiteralPath $keyPath).GetValue('')
        if ($registeredPath) {
            $registeredPath = [Environment]::ExpandEnvironmentVariables($registeredPath.Trim('"'))
        }
        if ($registeredPath -and (Test-Path -LiteralPath $registeredPath -PathType Leaf)) {
            $edgeExecutable = $registeredPath
            break
        }
    }
}

if (-not $edgeExecutable) {
    throw 'Microsoft Edge nao foi localizado pelo registro. Nenhum caminho presumido foi usado.'
}

$existingListener = Get-NetTCPConnection -LocalPort 9224 -State Listen -ErrorAction SilentlyContinue
if ($existingListener) {
    throw 'A porta TCP 9224 ja esta em uso. Nenhum navegador foi iniciado.'
}

$userDataDir = Join-Path $env:LOCALAPPDATA 'AurenScout\EdgeProfile-V3'
New-Item -ItemType Directory -Path $userDataDir -Force | Out-Null

$edgeArguments = @(
    '--remote-debugging-address=127.0.0.1',
    '--remote-debugging-port=9224',
    "--user-data-dir=`"$userDataDir`"",
    '--no-first-run'
)

Start-Process -FilePath $edgeExecutable -ArgumentList $edgeArguments
Write-Output 'Microsoft Edge V3 iniciado com CDP somente em 127.0.0.1:9224.'
Write-Output "Perfil dedicado do Scout V3: $userDataDir"
Write-Output 'Faca o login manualmente. Este script nao copia nem acessa outros perfis.'