$ErrorActionPreference = "Stop"
$VenvPython = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
$BridgeDir  = $PSScriptRoot

Set-Location $BridgeDir

if (-not (Test-Path ".env")) {
    Write-Error ".env not found. Copy .env.example to .env and fill in credentials."
    exit 1
}

Write-Host "Checking dependencies..." -ForegroundColor Cyan
& $VenvPython -m pip install -q -r requirements.txt

Write-Host "Starting desk mic (push-to-talk)..." -ForegroundColor Green
& $VenvPython desk_mic.py
