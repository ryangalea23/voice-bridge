"""Procedural keyboard-typing sound as 8kHz mu-law, for the 'working' state.

No audio asset and no ffmpeg: keystrokes are synthesised as short bursts of
decaying filtered noise and encoded to mu-law directly. A few seconds of loop
audio is built once at import and served in 200ms chunks, matching the chunk
size the Twilio media stream expects.

Volume is deliberately low. This is a background texture that says "still
working", not a sound effect.
"""
import math
import os
import random
import struct

CHUNK_BYTES = 3200          # 200ms of 8kHz mu-law, same as tts.py
SAMPLE_RATE = 8000
LOOP_SECONDS = 6.0          # long enough that the loop is not obvious
AMPLITUDE = int(os.environ.get("TYPING_AMPLITUDE", "4200"))  # of 32767

_BIAS = 0x84
_CLIP = 32635


def _linear_to_ulaw(sample: int) -> int:
    """Encode one signed 16-bit PCM sample as a mu-law byte (G.711)."""
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    if sample > _CLIP:
        sample = _CLIP
    sample += _BIAS

    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1

    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _keystroke(pcm: list[int], at: int, rng: random.Random) -> None:
    """Write one keystroke into the PCM buffer at sample offset `at`.

    A keystroke is a short noise burst with a fast exponential decay, plus a
    quieter second burst for the key coming back up.
    """
    for burst, (length_ms, gain) in enumerate(((18, 1.0), (10, 0.35))):
        start = at + (0 if burst == 0 else int(0.045 * SAMPLE_RATE))
        length = int(length_ms * SAMPLE_RATE / 1000)
        for i in range(length):
            idx = start + i
            if idx >= len(pcm):
                return
            decay = math.exp(-6.0 * i / length)
            noise = rng.uniform(-1.0, 1.0)
            pcm[idx] += int(AMPLITUDE * gain * decay * noise)


def _build_loop() -> bytes:
    """Build the mu-law typing loop once."""
    rng = random.Random(20260915)
    total = int(LOOP_SECONDS * SAMPLE_RATE)
    pcm = [0] * total

    at = 0
    while at < total:
        _keystroke(pcm, at, rng)
        # Human-ish rhythm: mostly fast, with the occasional pause for thought.
        gap_ms = rng.choice([70, 85, 95, 110, 130, 160, 240, 380])
        at += int(gap_ms * SAMPLE_RATE / 1000)

    return bytes(_linear_to_ulaw(max(-32768, min(32767, s))) for s in pcm)


_LOOP = _build_loop()
_LOOP_LEN = len(_LOOP)


def chunks(start_offset: int = 0):
    """Yield 200ms mu-law chunks of typing audio, looping forever.

    The caller paces playback; this only supplies bytes. `start_offset` lets
    each run begin at a different point so repeated turns do not sound
    identical.
    """
    pos = start_offset % _LOOP_LEN
    while True:
        end = pos + CHUNK_BYTES
        if end <= _LOOP_LEN:
            yield _LOOP[pos:end]
            pos = end % _LOOP_LEN
        else:
            wrap = end - _LOOP_LEN
            yield _LOOP[pos:] + _LOOP[:wrap]
            pos = wrap


def random_offset() -> int:
    return random.randrange(0, _LOOP_LEN)
