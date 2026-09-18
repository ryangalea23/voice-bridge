# window-handle.ps1 - find a window that can receive typed text.
#
# inject.py types into a window handle that claude-voice.ps1 saves. The obvious
# handle is GetConsoleWindow(), and in a classic conhost window that is the
# right one. Under Tabby, Windows Terminal and VS Code the shell runs through
# ConPTY, and GetConsoleWindow() returns a real but HIDDEN pseudo-console
# window: IsWindow says true, IsWindowVisible says false, the title is empty.
# SetForegroundWindow on it fails with error 0, so every injection is lost and
# the caller hears "session not active".
#
# So: use the console window when it is visible, otherwise walk up the parent
# process chain (pwsh -> Tabby) and take the first ancestor with a visible main
# window. Typed text then goes to the terminal app, which forwards it to
# whichever TAB is in front - the caller must keep the Claude tab active.
#
# Dot-source this file to get Resolve-VoiceWindow. check-window.ps1 runs the
# same function, so the check and the launcher can never drift apart.

if (-not ('VoiceWin' -as [type])) {
    Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class VoiceWin {
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int count);
    public static string Title(IntPtr hWnd) {
        StringBuilder sb = new StringBuilder(512);
        GetWindowTextW(hWnd, sb, sb.Capacity);
        return sb.ToString();
    }
}
"@
}

# Spoken out loud by the bridge when no usable window was found, so it is one
# plain sentence, not a stack trace.
$script:VoiceWindowSpokenError = "This terminal cannot receive typed text. Start the voice launcher in a plain console window, then call back."

function Get-ParentProcessId {
    param([int]$ProcessId)
    try {
        $row = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        if ($row) { return [int]$row.ParentProcessId }
    } catch {}
    return 0
}

function Resolve-VoiceWindow {
    <#
      Returns an object describing the window to type into:
        Hwnd           IntPtr, zero when nothing usable was found
        Kind           'console' | 'terminal' | 'none'
        ProcessName    owner of that window
        ConsoleHwnd    what GetConsoleWindow() returned
        ConsoleVisible whether that console window is visible
        Message        one line to print, and to speak when Kind is 'none'
    #>
    param([int]$MaxDepth = 6)

    $console = [VoiceWin]::GetConsoleWindow()
    $consoleVisible = ($console -ne [IntPtr]::Zero) -and [VoiceWin]::IsWindowVisible($console)

    if ($consoleVisible) {
        return [pscustomobject]@{
            Hwnd           = $console
            Kind           = 'console'
            ProcessName    = (Get-Process -Id $PID).ProcessName
            ConsoleHwnd    = $console
            ConsoleVisible = $true
            Message        = "Console window is visible - typing straight into this console."
        }
    }

    # ConPTY. Climb pwsh -> ... -> the terminal app and take its visible window.
    $pidCursor = $PID
    for ($depth = 0; $depth -lt $MaxDepth; $depth++) {
        $proc = $null
        try { $proc = Get-Process -Id $pidCursor -ErrorAction Stop } catch {}
        if ($proc) {
            $handle = $proc.MainWindowHandle
            if ($handle -ne [IntPtr]::Zero -and $handle -ne $console -and
                [VoiceWin]::IsWindow($handle) -and [VoiceWin]::IsWindowVisible($handle)) {
                return [pscustomobject]@{
                    Hwnd           = $handle
                    Kind           = 'terminal'
                    ProcessName    = $proc.ProcessName
                    ConsoleHwnd    = $console
                    ConsoleVisible = $false
                    Message        = "Console window is hidden (ConPTY) - using the $($proc.ProcessName) window instead."
                }
            }
        }
        $pidCursor = Get-ParentProcessId -ProcessId $pidCursor
        if ($pidCursor -le 0) { break }
    }

    return [pscustomobject]@{
        Hwnd           = [IntPtr]::Zero
        Kind           = 'none'
        ProcessName    = ''
        ConsoleHwnd    = $console
        ConsoleVisible = $false
        Message        = $script:VoiceWindowSpokenError
    }
}
