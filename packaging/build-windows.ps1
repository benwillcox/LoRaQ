param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") {
    throw "Build the Windows executable on Windows."
}

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $buildEnv = Join-Path $root "build\windows-env"
    if (-not (Test-Path -LiteralPath $buildEnv)) {
        & $Python -m venv "$buildEnv"
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create the build environment. Select a Python installation with venv support using -Python."
        }
    }
    $Python = Join-Path $buildEnv "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Invalid build environment at $buildEnv. Move it aside and retry."
    }
    & $Python -m pip install --only-binary=pygame-ce -r (Join-Path $PSScriptRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Build dependency installation failed. Check the pip error above."
    }
    & $Python -c "import sys, pygame, serial.tools.list_ports, pythonosc, PyInstaller; print('Building with:', sys.executable)"
    if ($LASTEXITCODE -ne 0) {
        throw "Runtime imports failed in the build environment."
    }
    $icon = Join-Path $PSScriptRoot "windows\loraq.ico"
    $artwork = Join-Path $root "assets"
    $apps = @("LoRaQ-transmitter", "LoRaQ-display", "LoRaQ-adapter-configure")
    foreach ($app in $apps) {
        & $Python -m PyInstaller --noconfirm --clean --onefile --console --name "$app" --icon "$icon" --add-data "${artwork};assets" --distpath dist\windows --workpath build\windows --specpath build "$app.py"
        if ($LASTEXITCODE -ne 0) {
            throw "$app build failed."
        }
    }
    $config = Join-Path $root "dist\windows\loraq.ini"
    if (-not (Test-Path -LiteralPath $config)) {
        Copy-Item -LiteralPath (Join-Path $root "loraq.ini") -Destination $config
    }
    foreach ($app in $apps) {
        Write-Output "Built $(Join-Path $root "dist\windows\$app.exe")"
    }
    Write-Output "Configuration: $config"
}
finally {
    Pop-Location
}
