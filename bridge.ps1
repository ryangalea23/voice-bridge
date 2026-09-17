param([switch]$NoTunnel)

$ErrorActionPreference = "Stop"
$BridgeDir = $PSScriptRoot

# Use a venv inside this folder, and build it on first run.
$VenvPython = Join-Path $BridgeDir "venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating venv..." -ForegroundColor Cyan
    & python -m venv (Join-Path $BridgeDir "venv")
    if (-not (Test-Path $VenvPython)) {
        Write-Error "Could not create the venv. Is Python 3.10+ on PATH?"
        exit 1
    }
}

# cloudflared: from PATH, or set $env:CLOUDFLARED_PATH to point at it.
$Cloudflared = if ($env:CLOUDFLARED_PATH) {
    $env:CLOUDFLARED_PATH
} elseif (Get-Command cloudflared -ErrorAction SilentlyContinue) {
    (Get-Command cloudflared).Source
} else {
    "cloudflared"
}

Set-Location $BridgeDir

if (-not (Test-Path ".env")) {
    Write-Error ".env not found. Copy .env.example to .env and fill in credentials."
    exit 1
}

$EnvVars = @{}
Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*([^#\s][^=]*?)\s*=\s*(.+?)\s*$') {
        $EnvVars[$matches[1].Trim()] = $matches[2].Trim().Trim('"').Trim("'")
    }
}

$AccountSid = $EnvVars['TWILIO_ACCOUNT_SID']
$AuthToken  = $EnvVars['TWILIO_AUTH_TOKEN']
$PhoneSid   = $EnvVars['TWILIO_PHONE_SID']

Write-Host "Checking dependencies..." -ForegroundColor Cyan
& $VenvPython -m pip install -q -r requirements.txt

if (-not $NoTunnel) {
    Write-Host "Starting Cloudflare Tunnel..." -ForegroundColor Cyan

    Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

    $cfLog = "$env:TEMP\cloudflared-bridge.log"
    Remove-Item $cfLog -Force -ErrorAction SilentlyContinue

    Start-Process $Cloudflared -ArgumentList "tunnel","--url","http://localhost:8000" -RedirectStandardError $cfLog -PassThru -WindowStyle Hidden | Out-Null

    $tunnelUrl = $null
    $deadline  = (Get-Date).AddSeconds(30)
    Write-Host "Waiting for tunnel URL..." -ForegroundColor Yellow

    while ((-not $tunnelUrl) -and ((Get-Date) -lt $deadline)) {
        Start-Sleep -Milliseconds 500
        if (Test-Path $cfLog) {
            $content = Get-Content $cfLog -Raw -ErrorAction SilentlyContinue
            if ($content -match 'https://[a-z0-9\-]+\.trycloudflare\.com') {
                $tunnelUrl = $Matches[0]
            }
        }
    }

    if (-not $tunnelUrl) {
        Write-Warning "Timed out waiting for tunnel URL. Continuing without webhook update."
    } else {
        Write-Host "Tunnel live: $tunnelUrl" -ForegroundColor Green
        [System.IO.File]::WriteAllText("$env:USERPROFILE\.claude\voice-tunnel.url", $tunnelUrl)

        if ($PhoneSid -and $AccountSid -and ($AccountSid -ne 'ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')) {
            $webhookUrl = $tunnelUrl + "/twilio/voice"
            $creds = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($AccountSid + ":" + $AuthToken))
            try {
                Invoke-RestMethod `
                    -Uri ("https://api.twilio.com/2010-04-01/Accounts/" + $AccountSid + "/IncomingPhoneNumbers/" + $PhoneSid + ".json") `
                    -Method Post `
                    -Headers @{ Authorization = "Basic " + $creds } `
                    -Body @{ VoiceUrl = $webhookUrl; VoiceMethod = "POST" } | Out-Null
                Write-Host "Twilio webhook updated: $webhookUrl" -ForegroundColor Green
            } catch {
                Write-Warning "Twilio update failed: $_"
                Write-Host "Set manually: $webhookUrl" -ForegroundColor Yellow
            }
        } else {
            Write-Warning "TWILIO_PHONE_SID not set. Set webhook manually: $tunnelUrl/twilio/voice"
        }
    }
}

Write-Host "Starting bridge on http://localhost:8000 ..." -ForegroundColor Green
& $VenvPython -m uvicorn bridge:app --host 0.0.0.0 --port 8000 --log-level info
