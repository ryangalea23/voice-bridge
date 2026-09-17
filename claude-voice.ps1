# claude-voice.ps1 — Launch Claude Code for voice bridge use.
# Registers this terminal with the bridge, sets HWND for inject.py.

param([string]$WorkDir = "")

if ($WorkDir -and (Test-Path $WorkDir)) {
    Set-Location $WorkDir
}

# The bridge gates /session/register and /session/deregister with a shared
# secret. Read it from the bridge's own .env so there is one source of truth.
# Override the location with $env:VOICE_BRIDGE_DIR if the bridge lives elsewhere.
# Where voice-bridge is checked out. Set $env:VOICE_BRIDGE_DIR if it is
# somewhere else; the default assumes this script sits next to it.
$bridgeDir = if ($env:VOICE_BRIDGE_DIR) { $env:VOICE_BRIDGE_DIR } else { $PSScriptRoot }
# voice-prompt.ps1 sits next to this script, or in the bridge folder if this
# script was copied somewhere else.
$promptLib = Join-Path $PSScriptRoot "voice-prompt.ps1"
if (-not (Test-Path $promptLib)) { $promptLib = Join-Path $bridgeDir "voice-prompt.ps1" }
. $promptLib
$bridgeEnv = Join-Path $bridgeDir ".env"
$bridgeVars = Read-BridgeEnv $bridgeEnv
$bridgeToken = if ($bridgeVars['SESSION_TOKEN']) { $bridgeVars['SESSION_TOKEN'] } else { "" }
if (-not $bridgeToken) {
    Write-Host "No SESSION_TOKEN in $bridgeEnv - the bridge will reject this session" -ForegroundColor Yellow
}

# Assign a session number from a shared counter file.
$counterFile = "$env:USERPROFILE\.claude\voice-session-counter"
$sessionNum = if (Test-Path $counterFile) { [int](Get-Content $counterFile -Raw) + 1 } else { 1 }
[System.IO.File]::WriteAllText($counterFile, $sessionNum.ToString())

# Get this console window's HWND.
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class ConsoleHelper {
    [DllImport("kernel32.dll")]
    public static extern IntPtr GetConsoleWindow();
}
"@
$hwnd = [ConsoleHelper]::GetConsoleWindow()

# Write HWND — shared file (active session) + per-session file.
$hwndFile        = "$env:USERPROFILE\.claude\voice-session.hwnd"
$sessionHwndFile = "$env:USERPROFILE\.claude\voice-session-$sessionNum.hwnd"
[System.IO.File]::WriteAllText($hwndFile, $hwnd.ToString())
[System.IO.File]::WriteAllText($sessionHwndFile, $hwnd.ToString())

Write-Host "Voice session #$sessionNum active (HWND: $hwnd)" -ForegroundColor Green

# Register with bridge (best-effort — bridge may not be running yet).
try {
    $body = @{ session_num = $sessionNum; hwnd = $hwnd.ToString() } | ConvertTo-Json
    Invoke-RestMethod -Uri "http://localhost:8000/session/register" `
        -Method Post -ContentType "application/json" -Body $body -TimeoutSec 2 `
        -Headers @{ "X-Bridge-Token" = $bridgeToken } | Out-Null
    Write-Host "Registered with bridge as terminal $sessionNum" -ForegroundColor Cyan
} catch {
    Write-Host "Bridge not running — will register on first stop hook" -ForegroundColor Yellow
}

$env:CLAUDE_VOICE_SESSION = "1"
$env:CLAUDE_SESSION_NUM   = $sessionNum.ToString()

# Voice-specific rules for the model: confirm risky actions, act as coordinator.
# These are instructions to the model, not a technical block.
$confirmRisky = Get-OnOffSetting $bridgeVars 'CONFIRM_RISKY'
$coordinator  = Get-OnOffSetting $bridgeVars 'VOICE_COORDINATOR'
Write-Host "CONFIRM_RISKY=$(if ($confirmRisky) {'on'} else {'off'}) VOICE_COORDINATOR=$(if ($coordinator) {'on'} else {'off'})" -ForegroundColor Cyan
$voicePrompt = Build-VoiceSystemPrompt $confirmRisky $coordinator
$claudeArgs = @('--dangerously-skip-permissions')
if ($voicePrompt) { $claudeArgs += @('--append-system-prompt', $voicePrompt) }

try {
    & claude @claudeArgs @args
} finally {
    # Deregister from bridge.
    try {
        $body = @{ session_num = $sessionNum } | ConvertTo-Json
        Invoke-RestMethod -Uri "http://localhost:8000/session/deregister" `
            -Method Post -ContentType "application/json" -Body $body -TimeoutSec 2 `
            -Headers @{ "X-Bridge-Token" = $bridgeToken } | Out-Null
    } catch {}

    $env:CLAUDE_VOICE_SESSION = ""
    $env:CLAUDE_SESSION_NUM   = ""
    Remove-Item $hwndFile        -ErrorAction SilentlyContinue
    Remove-Item $sessionHwndFile -ErrorAction SilentlyContinue
}
