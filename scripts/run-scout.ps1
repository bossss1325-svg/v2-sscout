$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw 'O Python Launcher do Windows (py) nao foi encontrado.'
    }

    & py -m scout
    $scoutExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $scoutExitCode
