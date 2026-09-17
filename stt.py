"""Deepgram streaming STT wrapper for Twilio 8kHz μ-law audio.

Uses utterance_end_ms instead of endpointing — works correctly on phone calls
where comfort noise prevents true silence detection.

encoding/sample_rate/channels are constructor params (default mulaw/8000/1,
unchanged for the phone bridge) so other callers — e.g. desk_mic.py, which
sends 16kHz mono linear PCM from a local microphone — can reuse this class
without duplicating the Deepgram event wiring.

on_utterance_full is an optional second callback, (text, confidence) instead
of just (text). Deepgram reports a confidence score per transcript segment;
bridge.py's phone path never needed it, so on_utterance's signature is left
untouched. desk_mic.py uses on_utterance_full to drop low-confidence
transcripts (background noise, other people talking) without guessing at
what was said.
"""
import logging
from typing import Awaitable, Callable

from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents

log = logging.getLogger(__name__)


class DeepgramSTT:
    def __init__(
        self,
        on_utterance: Callable[[str], Awaitable[None]] | None = None,
        on_speech_started: Callable[[], Awaitable[None]] | None = None,
        encoding: str = "mulaw",
        sample_rate: int = 8000,
        channels: int = 1,
        on_utterance_full: Callable[[str, float], Awaitable[None]] | None = None,
    ):
        if on_utterance is None and on_utterance_full is None:
            raise ValueError("DeepgramSTT needs on_utterance or on_utterance_full")
        self._on_utterance = on_utterance
        self._on_utterance_full = on_utterance_full
        self._on_speech_started = on_speech_started
        self._encoding = encoding
        self._sample_rate = sample_rate
        self._channels = channels
        self._conn = None
        self._accumulated: str = ""  # confirmed is_final segments
        self._current: str = ""      # latest interim
        self._confidences: list[float] = []  # confidence of each buffered is_final segment
        self._current_confidence: float = 1.0

    async def start(self, api_key: str) -> None:
        client = DeepgramClient(api_key)
        self._conn = client.listen.asyncwebsocket.v("1")

        async def _on_message(self_conn, result, **kwargs):
            try:
                alt = result.channel.alternatives[0]
                transcript = alt.transcript.strip()
                if not transcript:
                    return
                if result.is_final:
                    # Accumulate — don't fire yet, wait for UtteranceEnd
                    sep = " " if self._accumulated else ""
                    self._accumulated += sep + transcript
                    self._confidences.append(getattr(alt, "confidence", 1.0))
                    self._current = ""
                    log.info("STT is_final (buffered): %r → accumulated: %r", transcript, self._accumulated)
                else:
                    self._current = transcript
                    self._current_confidence = getattr(alt, "confidence", 1.0)
                    log.info("STT interim: %r", transcript)
                    # Interim = user is actively speaking — trigger interruption
                    if self._on_speech_started:
                        await self._on_speech_started()
            except Exception as exc:
                log.debug("STT message parse error: %s", exc)

        async def _on_utterance_end(self_conn, utterance_end, **kwargs):
            full = (self._accumulated + " " + self._current).strip()
            confidences = list(self._confidences)
            if self._current:
                confidences.append(self._current_confidence)
            # Conservative: the weakest segment sets the confidence for the
            # whole utterance, rather than averaging over a run of clear words.
            confidence = min(confidences) if confidences else 1.0
            log.info("STT UtteranceEnd — firing: %r (confidence=%.2f)", full, confidence)
            self._accumulated = ""
            self._current = ""
            self._confidences = []
            self._current_confidence = 1.0
            if not full:
                return
            if self._on_utterance_full:
                await self._on_utterance_full(full, confidence)
            elif self._on_utterance:
                await self._on_utterance(full)

        async def _on_error(self_conn, error, **kwargs):
            log.warning("Deepgram error: %s", error)

        self._conn.on(LiveTranscriptionEvents.Transcript, _on_message)
        self._conn.on(LiveTranscriptionEvents.UtteranceEnd, _on_utterance_end)
        self._conn.on(LiveTranscriptionEvents.Error, _on_error)

        opts = LiveOptions(
            model="nova-2",
            language="en-US",
            encoding=self._encoding,
            sample_rate=self._sample_rate,
            channels=self._channels,
            interim_results=True,    # needed for utterance_end_ms buffering
            vad_events=True,         # enables SpeechStarted + UtteranceEnd
            utterance_end_ms="2000", # 2s without new words = end of utterance
            endpointing=False,       # disable silence-based endpointing
        )
        started = await self._conn.start(opts)
        if not started:
            raise RuntimeError("Deepgram connection failed to start")
        log.info("Deepgram connection open")

    async def send(self, audio_bytes: bytes) -> None:
        if self._conn:
            await self._conn.send(audio_bytes)

    async def finish(self) -> None:
        if self._conn:
            await self._conn.finish()
            self._conn = None
            log.info("Deepgram connection closed")
