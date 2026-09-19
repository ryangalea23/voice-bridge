<#
.SYNOPSIS
  Place an outbound call that rings you and hands the line to Claude Code, with
  answering-machine detection turned on.

.DESCRIPTION
  The bridge itself only answers calls. This script is the other direction: it
  asks Twilio to dial a number and point the answered call at /twilio/voice, the
  same webhook an inbound call uses.

  MachineDetection is the part that matters. With it on, Twilio works out what
  picked up and sends the answer in AnsweredBy with the webhook. If a machine
  answered, the bridge hangs up and types nothing - without it, a voicemail
  greeting gets transcribed and typed into the session as a prompt. That really
  happened, which is why this script exists instead of a hand-written curl.

  Credentials and the public URL are read from .env next to this script.
  The number you dial must be in ALLOWED_CALLERS, the same as for an inbound
  call, or the bridge hangs up on it.

.PARAMETER To
  The number to ring, in E.164 (+15551112222). Must be in ALLOWED_CALLERS.

.PARAMETER From
  The Twilio number to call from, in E.164. Looked up from TWILIO_PHONE_SID
  when left out.

.PARAMETER BridgeUrl
  Public https URL of the bridge, no trailing slash. Defaults to BRIDGE_URL in
  .env, then to the tunnel URL bridge.ps1 leaves in ~\.claude\voice-tunnel.url.

.PARAMETER MachineDetection
  Enable decides as soon as it can tell, which is what you want for a call you
  intend to talk on. DetectMessageEnd waits for a greeting to finish, which is
  only useful if you mean to leave a message.

.PARAMETER MachineDetectionTimeout
  Seconds Twilio may spend deciding before it gives up and reports unknown.
  A person who answers waits this long at worst, so keep it short.

.EXAMPLE
  .\outbound-call.ps1 -To +15551112222
#>
param(
    [Parameter(Mandatory = $true)][string]$To,
    [string]$From,
    [string]$BridgeUrl,
    [ValidateSet("Enable", "DetectMessageEnd")][string]$MachineDetection = "Enable",
    [int]$MachineDetectionTimeout = 15
)

$ErrorActionPreference = "Stop"
$BridgeDir = $PSScriptRoot

$EnvFile = Join-Path $BridgeDir ".env"
if (-not (Test-Path $EnvFile)) {
    Write-Error ".env not found. Copy .env.example to .env and fill in credentials."
    exit 1
}

$EnvVars = @{}
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^\s*([^#\s][^=]*?)\s*=\s*(.+?)\s*$') {
        $EnvVars[$matches[1].Trim()] = $matches[2].Trim().Trim('"').Trim("'")
    }
}

$AccountSid = $EnvVars['TWILIO_ACCOUNT_SID']
$AuthToken  = $EnvVars['TWILIO_AUTH_TOKEN']
$PhoneSid   = $EnvVars['TWILIO_PHONE_SID']

if (-not $AccountSid -or -not $AuthToken) {
    Write-Error "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set in .env"
    exit 1
}

$creds = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($AccountSid + ":" + $AuthToken))
$headers = @{ Authorization = "Basic " + $creds }

# Where the bridge is reachable from the internet.
if (-not $BridgeUrl) { $BridgeUrl = $EnvVars['BRIDGE_URL'] }
if (-not $BridgeUrl) {
    $tunnelFile = Join-Path $env:USERPROFILE ".claude\voice-tunnel.url"
    if (Test-Path $tunnelFile) { $BridgeUrl = (Get-Content $tunnelFile -Raw).Trim() }
}
if (-not $BridgeUrl) {
    Write-Error "No public URL. Pass -BridgeUrl, or set BRIDGE_URL in .env, or start bridge.ps1 with its tunnel."
    exit 1
}
$BridgeUrl = $BridgeUrl.TrimEnd('/')

# The Twilio number to call from. Looked up from the SID so there is one less
# thing to keep in step in .env.
if (-not $From) {
    if (-not $PhoneSid) {
        Write-Error "Pass -From, or set TWILIO_PHONE_SID in .env so the number can be looked up."
        exit 1
    }
    $numberUrl = "https://api.twilio.com/2010-04-01/Accounts/$AccountSid/IncomingPhoneNumbers/$PhoneSid.json"
    $From = (Invoke-RestMethod -Uri $numberUrl -Headers $headers).phone_number
}

Write-Host "Calling $To from $From" -ForegroundColor Cyan
Write-Host "Answer webhook: $BridgeUrl/twilio/voice" -ForegroundColor DarkGray
Write-Host "Machine detection: $MachineDetection (timeout ${MachineDetectionTimeout}s)" -ForegroundColor DarkGray

$body = @{
    To                      = $To
    From                    = $From
    Url                     = "$BridgeUrl/twilio/voice"
    Method                  = "POST"
    MachineDetection        = $MachineDetection
    MachineDetectionTimeout = $MachineDetectionTimeout
}

try {
    $call = Invoke-RestMethod `
        -Uri "https://api.twilio.com/2010-04-01/Accounts/$AccountSid/Calls.json" `
        -Method Post -Headers $headers -Body $body
} catch {
    Write-Error "Twilio refused the call: $_"
    exit 1
}

Write-Host "Call placed: $($call.sid) ($($call.status))" -ForegroundColor Green
Write-Host "If voicemail picks up, the bridge hangs up and types nothing. Watch the log for 'answered by'." -ForegroundColor DarkGray
