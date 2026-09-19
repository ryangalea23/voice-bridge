# voice-prompt.ps1 - Builds the extra system prompt for a voice session.
# Dot-sourced by claude-voice.ps1. Kept separate so it can be tested without
# launching claude.

function Read-BridgeEnv([string]$Path) {
    $vars = @{}
    if (Test-Path $Path) {
        foreach ($line in Get-Content $Path) {
            if ($line -match '^\s*([^#\s][^=]*?)\s*=\s*(.+?)\s*$') {
                $vars[$Matches[1].Trim()] = $Matches[2].Trim().Trim('"').Trim("'")
            }
        }
    }
    return $vars
}

# on|off setting with a default. Anything else falls back to the default and
# says so, so a typo never quietly turns a safety rule off.
function Get-OnOffSetting($Vars, [string]$Name, [bool]$Default = $true) {
    $raw = $Vars[$Name]
    if (-not $raw) { return $Default }
    switch ($raw.ToLower()) {
        'on'  { return $true }
        'off' { return $false }
        default {
            $d = if ($Default) { 'on' } else { 'off' }
            Write-Host "$Name=$raw is not on or off - using $d" -ForegroundColor Yellow
            return $Default
        }
    }
}

# No double quotes in this text: Windows PowerShell 5.1 mangles them when
# passing arguments to a native program.
function Build-VoiceSystemPrompt([bool]$ConfirmRisky, [bool]$Coordinator, [bool]$Narrate = $true) {
    $parts = @()
    if ($ConfirmRisky) {
        $parts += "This session is driven by voice over a phone call, and speech-to-text can mishear. " +
            "Before any destructive or outward-facing action (deleting files or data, git push, force operations, " +
            "deploys, sending email or messages, spending money, changing production), say in one sentence exactly " +
            "what you are about to do and wait for an explicit yes in the next message. " +
            "If a request looks garbled or ambiguous, ask a short clarifying question instead of guessing."
    }
    if ($Coordinator) {
        $parts += "Act as a coordinator. Answer the caller right away in one or two short spoken sentences. " +
            "Hand any work expected to take more than about 10 seconds to background agents or background shell tasks " +
            "instead of doing it inline, and report each result in one sentence when it lands. " +
            "Keep replies short and speakable: no tables, no code blocks, no long lists."
    }
    if ($Narrate) {
        $parts += "The caller hears everything you write, read out loud as you write it, so your text is the only thing " +
            "filling the line. Start every reply with one short spoken sentence that shows you understood the request, " +
            "before you use any tool, so the caller is never sitting in silence. " +
            "When the next step will take more than a moment, say one short line about what you are about to do, then do it. " +
            "One sentence at a time, plain spoken English, no lists, no markdown, and never read a file path out loud. " +
            "Do not narrate quick steps, and do not say the same thing twice."
    }
    return ($parts -join ' ')
}
