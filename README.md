# Voice Bridge

Call a phone number and talk to Claude Code. It listens, types what you said into a real
Claude Code session, and reads the answer back to you.

Built for driving. You get in the car, call your own number, and keep working on whatever
the agent was doing. No screen, no keyboard, no passenger seat laptop.

It is a real terminal on the other end, not a chatbot. Whatever Claude Code can do in that
window, it does while you are talking to it.

> **Windows only.** The piece that types into the terminal uses Win32 APIs. See
> [Why Windows only](#why-windows-only).

## How it works

```
  your phone
      |  call
      v
  Twilio  ──webhook──>  Cloudflare Tunnel  ──>  bridge.py (FastAPI, localhost:8000)
      |                                              |
      |<── mu-law audio over a Media Stream ─────────┤
                                                     |
                          Deepgram (speech to text)  |
                                                     v
                                      inject.py types into the
                                      claude-voice terminal window
                                                     |
                          edge-tts (text to speech)  |
                                                     v
                                        spoken back down the call
```

While a turn is running you hear a faint keyboard-typing sound, synthesised on the fly,
so the line does not go silent while the agent is thinking.

## Pairs with a coordinator setup

This is most useful when the session you are calling is a **coordinator** rather than a
worker. Mine is a Claude Code session I call Captain: it holds the plan, dispatches work
to other sessions and subagents, and stays responsive instead of doing long jobs itself.

That split is what makes a phone call work. A coordinator can answer in one line and hand
the real work off, which is exactly what you want when the only interface you have is your
voice and you are watching the road. Calling a session that is head down in a long build
just means listening to silence.

*(Captain will get its own repo. Link to follow.)*

You do not need your own coordinator to get this behavior. With `VOICE_COORDINATOR=on`
(the default), `claude-voice.ps1` tells the session to answer in one or two short spoken
sentences, hand anything longer than about 10 seconds to background agents or background
shell tasks, and report each result in one sentence when it lands. Set it to `off` if you
want the session to work inline.

## Security

This thing types what a caller says into a live terminal and presses Enter. Treat it that
way. Three gates, and **all of them fail closed**, so a fresh install with no configuration
rejects everything rather than standing open.

**1. Caller allowlist.** Only numbers in `ALLOWED_CALLERS` get through. Everyone else hears
"This number is not authorized" and gets hung up on.

Twilio's signature check proves the *webhook* came from Twilio. It says nothing about who
dialled. Those are different questions, and only the second one matters here.

**2. Stream ticket.** The phone call and the audio stream are two separate connections. The
allowlist runs on the first one. Without something tying them together, anyone who learned
the tunnel URL could open the audio socket directly, send made-up media frames, and reach
the terminal without ever dialling, which would make the allowlist decorative.

So an accepted call mints a single-use ticket, good for two minutes, and it goes in the
stream URL. The socket is refused before it is accepted unless the ticket is present,
unused and unexpired. A rejected caller never gets one.

**3. Endpoint token.** `/session/register`, `/session/deregister` and `/health` require an
`X-Bridge-Token` header matching `SESSION_TOKEN`, compared in constant time. The tunnel
exposes every route, not just the Twilio ones, and `bridge.ps1` binds uvicorn to `0.0.0.0`,
so these are reachable from your network as well as the tunnel. `/health` is included
because it reports window handles and transcript paths, which is a map of your machine.

Numbers must be written in E.164, the `+15551112222` form Twilio sends. If an entry is
missing its country code the bridge says so at startup and names it, rather than silently
locking you out:

```
WARNING bridge ALLOWED_CALLERS entry '5551112222' does not look like E.164.
Write it as +15551112222, not 5551112222.
```

### Read this before you run it

**`claude-voice.ps1` launches Claude Code with `--dangerously-skip-permissions`.** Every
tool call runs with no confirmation, including shell commands. That is deliberate, because
a permission prompt you cannot see would stall every call, but it means an accepted caller
reaches an agent with no approval step in front of it. If that is not a trade you want,
edit that line and use a normal permission mode. You will have to approve things some
other way.

**What this still is not.** Anyone holding your phone can call. There is no PIN and no
per-call confirmation. Caller ID can be spoofed, so the allowlist raises the bar rather
than sealing the door. The gates above stop a stranger who finds your tunnel URL; they do
not stop someone who can spoof your number. For a personal machine that is a reasonable
trade. Do not point this at anything you would not hand a stranger a keyboard to.

## Mishearing and safety

Speech to text gets words wrong, and the bridge types what it heard straight into the
session. These settings give you a chance to catch a bad transcript. All of them live in
`.env` and are read once at startup.

**Read-back (`READBACK`).** Right after your words are typed in, the bridge tells you what
it heard.

- `transcript` (default): says "I heard:" and the cleaned transcript, cut to about 20
  words. The first read-back of a call also says "Say stop to cancel."
- `haiku`: asks Claude Haiku to restate your request in 15 words or fewer, and says that
  instead. It only restates, it does not answer. It needs `ANTHROPIC_API_KEY`. If the key
  is missing, the call errors, or it takes longer than `READBACK_HAIKU_TIMEOUT_MS`, that
  turn falls back to the transcript read-back. The Haiku call runs alongside the typing,
  so it never slows down getting your words into the session.
- `off`: a short canned "Got it." like before.

Read-back does not hold your words back. They are already in the session by the time you
hear them. It tells you what is running so you can stop it.

**Stop command.** Say just "stop", "cancel", "wait", "hold on" or "never mind" and the
bridge presses Escape in the session window to interrupt the current turn, stops the
typing sound and says "Stopped." The word is not typed in as a prompt. It only counts when
it is the whole thing you said, so "stop the server" is still sent as a normal request.
Change the list with `VOICE_STOP_WORDS`.

**Confirm risky actions (`CONFIRM_RISKY`).** On by default. `claude-voice.ps1` adds a line
to the session's system prompt: before anything destructive or outward-facing (deleting
files or data, git push, force operations, deploys, sending email or messages, spending
money, changing production), say in one sentence what it is about to do and wait for an
explicit yes. If a request looks garbled, ask instead of guessing. Be clear on what this
is: an instruction to the model, not a technical block. The session still runs with
`--dangerously-skip-permissions`, and nothing in the bridge stops a tool call the model
decides to make.

**Boost hard words (`DEEPGRAM_KEYTERMS`).** A comma list of names and jargon Deepgram
keeps getting wrong, such as your project names. They are sent to Deepgram as keywords
(the right option for the `nova-2` model the bridge uses).

## Setup

You need a Twilio number, a Deepgram key, `cloudflared`, and Python 3.10+.

```powershell
git clone https://github.com/ryangalea23/voice-bridge
cd voice-bridge
copy .env.example .env
```

Fill in `.env`:

| Setting | What it is |
|---|---|
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | From the Twilio console |
| `TWILIO_PHONE_SID` | Your number's SID, starts with `PN`. The bridge rewrites its webhook on every start. |
| `DEEPGRAM_API_KEY` | From the Deepgram console |
| `ALLOWED_CALLERS` | **Required.** Your mobile in E.164, comma separated for more. |
| `SESSION_TOKEN` | **Required.** `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `READBACK`, `VOICE_STOP_WORDS`, `CONFIRM_RISKY`, `VOICE_COORDINATOR`, `DEEPGRAM_KEYTERMS` | Optional. See [Mishearing and safety](#mishearing-and-safety) and `.env.example`. |

Then start it:

```powershell
.\bridge.ps1
```

First run builds a venv and installs dependencies. It opens a Cloudflare tunnel, points
your Twilio number's webhook at it, and serves on port 8000.

Add the Stop hook so Claude Code's replies actually reach the bridge. Open (or create)
`settings.json` for the session you will call from, and add `voice_hook.py` as a Stop
hook:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python C:/path/to/voice-bridge/voice_hook.py"
          }
        ]
      }
    ]
  }
}
```

Without this, Claude Code never tells the bridge what it said, so a call connects and the
agent works, but the caller hears nothing back.

In a second terminal, start a session for calls to land in:

```powershell
.\claude-voice.ps1
```

That registers the window so `inject.py` knows where to type. Without it a call connects
but reports no active session.

Under Tabby, Windows Terminal or VS Code the shell runs through ConPTY, so the console
window is real but hidden and cannot be brought to the front. The launcher notices that
and saves the terminal app's own window instead, then warns you that typed text goes to
whichever tab is in front - keep the Claude tab active during a call. Run
`.\check-window.ps1` in a terminal to see which window it would pick. If it finds none,
start the launcher in a classic console window:

```powershell
Start-Process conhost.exe -ArgumentList 'pwsh.exe','-NoExit','-Command','.\claude-voice.ps1'
```

Now call your number.

## Files

| File | Job |
|---|---|
| `bridge.py` | FastAPI app: Twilio webhook, media stream, the three security gates |
| `voice_hook.py` | Claude Code Stop hook: sends the reply text to the bridge |
| `stt.py` | Deepgram streaming speech to text |
| `tts.py` | edge-tts speech, encoded to 8kHz mu-law for the call |
| `typing_sound.py` | Synthesised keyboard sound so the line is not silent mid-turn |
| `inject.py` | Types text into the registered terminal window |
| `voice_text.py` | Strips filler words and spots stop commands before injecting |
| `voice_settings.py` | Reads the read-back, stop word and keyterm settings |
| `readback.py` | Builds the "I heard" read-back, including the Haiku restatement |
| `desk_mic.py` / `desk-mic.ps1` | Same thing from your desk mic, no phone call |
| `bridge.ps1` | Starts the tunnel, updates the Twilio webhook, runs the server |
| `claude-voice.ps1` | Launches a Claude Code session and registers its window |
| `window-handle.ps1` | Picks the window to type into: the console, or the terminal app under ConPTY |
| `check-window.ps1` | Prints which window would be used, without launching Claude Code |
| `voice-prompt.ps1` | Builds the confirm and coordinator system prompt text |
| `tests/` | pytest suite. No phone, Deepgram or network needed: `python -m pytest tests` |

## Why Windows only

`inject.py` gets text into the terminal by putting it on the clipboard and sending Ctrl+V
and Enter to a specific window handle. That needs `win32clipboard`, `win32gui`, and
`AttachThreadInput` to steal focus, because Windows blocks a background process from
calling `SetForegroundWindow` on its own.

Porting means rewriting that file for the target platform. The rest of the stack, Twilio,
Deepgram, FastAPI, edge-tts, is cross-platform already. Pull requests welcome.

## Known rough edges

- **It takes your clipboard.** Every injected message overwrites whatever you had copied.
- **It steals focus.** The registered window jumps to the front on each message.
- **One call at a time.** There is a single global call object, not a pool.
- **The tunnel URL changes** on every restart with a free Cloudflare quick tunnel, which is
  why `bridge.ps1` rewrites the Twilio webhook each time. A named tunnel avoids this.
- **Deepgram and Twilio both cost money** per minute. Small, not zero.

## License

MIT. See [LICENSE](LICENSE).
