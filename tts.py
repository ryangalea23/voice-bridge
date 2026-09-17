"""edge-tts → 8kHz μ-law transcode for Twilio Media Streams outbound audio."""
import asyncio
import logging
import os
from collections.abc import AsyncIterator

import edge_tts

log = logging.getLogger(__name__)

VOICE = os.environ.get("SPEAK_VOICE", "en-US-AriaNeural")
RATE = os.environ.get("SPEAK_RATE", "+15%")
FFMPEG = os.environ.get("FFMPEG_PATH", r"C:\ffmpeg\bin\ffmpeg.exe")
CHUNK_BYTES = 3200  # 200ms of 8kHz μ-law audio


async def _feed_ffmpeg(proc: asyncio.subprocess.Process, comm: edge_tts.Communicate) -> None:
    """Stream edge-tts MP3 bytes into ffmpeg's stdin as they're synthesized."""
    try:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                proc.stdin.write(chunk["data"])
                await proc.stdin.drain()
    except Exception as exc:
        log.error("edge-tts stream error: %s", exc)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass


async def text_to_mulaw_chunks(text: str) -> AsyncIterator[bytes]:
    """Synthesize text and yield raw μ-law chunks suitable for Twilio Media Streams.

    Streams MP3 from edge-tts directly into ffmpeg so audio starts before
    the full synthesis is complete.
    """
    comm = edge_tts.Communicate(text=text, voice=VOICE, rate=RATE)

    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-y",
        "-f", "mp3",
        "-i", "pipe:0",
        "-ar", "8000",
        "-ac", "1",
        "-acodec", "pcm_mulaw",
        "-f", "mulaw",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    feed_task = asyncio.create_task(_feed_ffmpeg(proc, comm))

    try:
        while True:
            chunk = await proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                break
            yield chunk
    except Exception as exc:
        log.error("TTS transcode error: %s", exc)
    finally:
        feed_task.cancel()
        try:
            await feed_task
        except asyncio.CancelledError:
            pass
        await proc.wait()
