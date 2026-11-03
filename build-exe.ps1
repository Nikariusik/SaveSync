$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$venvPath = Join-Path $projectRoot ".venv"
$pythonPath = Join-Path $venvPath "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonCommand = Get-Command python -ErrorAction Stop
    & $pythonCommand.Source -m venv $venvPath
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the project virtual environment."
    }
}

Push-Location $projectRoot
try {
    & $pythonPath -m pip install -r (Join-Path $projectRoot "requirements-build.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install executable build dependencies."
    }

    & $pythonPath -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath (Join-Path $projectRoot "dist") `
        --workpath (Join-Path $projectRoot "build") `
        (Join-Path $projectRoot "SaveSync.spec")
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed to build SaveSync.exe."
    }

    $executablePath = Join-Path $projectRoot "dist\SaveSync.exe"
    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
        throw "PyInstaller completed without producing SaveSync.exe."
    }
    Write-Host "Built: $executablePath"
    Write-Host "This executable is the desktop client; the API server runs separately."
}
finally {
    Pop-Location
}
