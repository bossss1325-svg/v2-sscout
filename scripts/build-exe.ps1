$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') {
    throw 'O PyInstaller deve ser executado no Windows para gerar dist/AurenScout-V2.exe. Builds nao sao cross-platform.'
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$outputDirectory = Join-Path $repoRoot 'dist'
$expectedExecutable = Join-Path $outputDirectory 'AurenScout-V2.exe'
Push-Location $repoRoot
try {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw 'O Python Launcher do Windows (py) nao foi encontrado. Instale Python para Windows para gerar o build.'
    }

    & py -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) {
        throw 'Falha ao instalar as dependencias de build.'
    }

    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path 'build/pyinstaller' -Force | Out-Null
    & py -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --console `
        --name AurenScout-V2 `
        --distpath $outputDirectory `
        --workpath build/pyinstaller `
        --specpath build/pyinstaller `
        --paths . `
        --collect-all playwright `
        scout/__main__.py
    if ($LASTEXITCODE -ne 0) {
        throw 'PyInstaller falhou ao empacotar o Scout.'
    }

    if (-not (Test-Path -LiteralPath $expectedExecutable -PathType Leaf)) {
        throw "Build terminou sem gerar o executavel esperado: $expectedExecutable"
    }

    $file = Get-Item -LiteralPath $expectedExecutable
    Write-Output "Build concluido: $($file.FullName) ($($file.Length) bytes)"
}
finally {
    Pop-Location
}
