# check-window.ps1 - print which window claude-voice.ps1 would type into.
#
# Runs the real resolver from window-handle.ps1 in the current process, without
# launching Claude Code or touching the saved handle files. Run it inside the
# terminal you use for calls: Tabby, Windows Terminal, VS Code, or a classic
# console window started with conhost.exe.

$lib = Join-Path $PSScriptRoot "window-handle.ps1"
. $lib

$w = Resolve-VoiceWindow

Write-Host "host process     : $((Get-Process -Id $PID).ProcessName) (pid $PID)"
$parentId = Get-ParentProcessId -ProcessId $PID
$parentName = try { (Get-Process -Id $parentId -ErrorAction Stop).ProcessName } catch { "unknown" }
Write-Host "parent process   : $parentName (pid $parentId)"
Write-Host "console hwnd     : $($w.ConsoleHwnd)"
Write-Host "console visible  : $($w.ConsoleVisible)"
Write-Host "console title    : '$([VoiceWin]::Title($w.ConsoleHwnd))'"
Write-Host "chosen hwnd      : $($w.Hwnd)"
Write-Host "chosen kind      : $($w.Kind)"
Write-Host "chosen owner     : $($w.ProcessName)"
$chosenVisible = if ($w.Hwnd -ne [IntPtr]::Zero) { [VoiceWin]::IsWindowVisible($w.Hwnd) } else { $false }
Write-Host "chosen visible   : $chosenVisible"
Write-Host "chosen title     : '$(if ($w.Hwnd -ne [IntPtr]::Zero) { [VoiceWin]::Title($w.Hwnd) } else { '' })'"
Write-Host ""
Write-Host "startup message  : $($w.Message)"
if ($w.Kind -eq 'terminal') {
    Write-Host "startup warning  : Typed text goes to whichever TAB is in front of $($w.ProcessName). Keep this tab active during a call."
}
if ($w.Kind -eq 'none') {
    Write-Host "startup warning  : No window can receive typed text. Voice injection would be OFF."
}
