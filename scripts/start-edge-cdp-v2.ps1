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
    throw 'Microsoft Edge nao foi localizado pelo registro do Windows. Nenhum caminho de instalacao presumido foi usado.'
}

$existingListener = Get-NetTCPConnection -LocalPort 9223 -State Listen -ErrorAction SilentlyContinue
if ($existingListener) {
    throw 'A porta TCP 9223 ja esta em uso. Nenhum navegador foi iniciado.'
}

$userDataDir = 'C:\Users\gabriela.pacheco\AppData\Local\AurenScout\EdgeProfile-V2'
New-Item -ItemType Directory -Path $userDataDir -Force | Out-Null

$edgeArguments = @(
    '--remote-debugging-address=127.0.0.1',
    '--remote-debugging-port=9223',
    "--user-data-dir=`"$userDataDir`"",
    '--no-first-run'
)

Start-Process -FilePath $edgeExecutable -ArgumentList $edgeArguments
Write-Output 'Microsoft Edge V2 iniciado com CDP somente em 127.0.0.1:9223.'
Write-Output "Perfil dedicado do Scout V2: $userDataDir"
Write-Output 'Faca o login manualmente no navegador. Este script nao copia nem acessa outro perfil.'
