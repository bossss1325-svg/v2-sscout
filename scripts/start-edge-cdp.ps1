param(
    [ValidateRange(1, 65535)]
    [int]$Port = 9222
)

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

$existingListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existingListener) {
    throw "A porta TCP $Port ja esta em uso. Nenhum navegador foi iniciado."
}

$userDataDir = Join-Path $env:LOCALAPPDATA 'AurenScout\EdgeProfile'
New-Item -ItemType Directory -Path $userDataDir -Force | Out-Null

$edgeArguments = @(
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=$Port",
    "--user-data-dir=`"$userDataDir`"",
    '--no-first-run'
)

Start-Process -FilePath $edgeExecutable -ArgumentList $edgeArguments
Write-Output "Microsoft Edge iniciado com CDP somente em 127.0.0.1:$Port."
Write-Output "Perfil dedicado do Scout: $userDataDir"
Write-Output 'Faca o login manualmente no navegador. Este script nao copia nem acessa outro perfil.'
