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

Then start it:

```powershell
.\bridge.ps1
```

First run builds a venv and installs dependencies. It opens a Cloudflare tunnel, points
your Twilio number's webhook at it, and serves on port 8000.

In a second terminal, start a session for calls to land in:

```powershell
.\claude-voice.ps1
```

That registers the window so `inject.py` knows where to type. Without it a call connects
but reports no active session.

Now call your number.

## Files

| File | Job |
|---|---|
| `bridge.py` | FastAPI app: Twilio webhook, media stream, the three security gates |
| `stt.py` | Deepgram streaming speech to text |
| `tts.py` | edge-tts speech, encoded to 8kHz mu-law for the call |
| `typing_sound.py` | Synthesised keyboard sound so the line is not silent mid-turn |
| `inject.py` | Types text into the registered terminal window |
| `voice_text.py` | Strips filler words before injecting |
| `desk_mic.py` / `desk-mic.ps1` | Same thing from your desk mic, no phone call |
| `bridge.ps1` | Starts the tunnel, updates the Twilio webhook, runs the server |
| `claude-voice.ps1` | Launches a Claude Code session and registers its window |

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
