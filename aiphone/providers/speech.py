"""Streaming speech-to-text (Deepgram) and text-to-speech (Deepgram, ElevenLabs,
OpenAI) used by the Claude pipeline. All audio in and out is 8 kHz int16 PCM."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import urlencode

import httpx
import websockets

from .. import audio

log = logging.getLogger("aiphone.speech")


# ------------------------------------------------------------ speech-to-text
class DeepgramSTT:
    """Streams caller audio to Deepgram and reports transcripts.

    ``on_interim(text)`` fires for every partial transcript (used for barge-in),
    ``on_final(text)`` for each finalised segment and ``on_turn_end()`` when
    Deepgram decides the caller has finished speaking.
    """

    URL = "wss://api.deepgram.com/v1/listen"

    def __init__(self, api_key: str, model: str, language: str, endpointing_ms: int,
                 on_interim: Callable[[str], Awaitable[None]],
                 on_final: Callable[[str], Awaitable[None]],
                 on_turn_end: Callable[[], Awaitable[None]]):
        self.api_key = api_key
        self.params = {
            "model": model or "nova-3",
            "language": language or "en",
            "encoding": "linear16",
            "sample_rate": audio.SAMPLE_RATE,
            "channels": 1,
            "interim_results": "true",
            "endpointing": int(endpointing_ms or 500),
            "utterance_end_ms": max(1000, int(endpointing_ms or 500) + 500),
            "vad_events": "true",
            "smart_format": "true",
            "punctuate": "true",
        }
        self.on_interim, self.on_final, self.on_turn_end = on_interim, on_final, on_turn_end
        self.ws = None
        self._task: asyncio.Task | None = None
        self._buf = bytearray()

    async def start(self) -> None:
        url = f"{self.URL}?{urlencode(self.params)}"
        self.ws = await websockets.connect(
            url, additional_headers={"Authorization": f"Token {self.api_key}"},
            max_size=None, open_timeout=15, ping_interval=20)
        self._task = asyncio.create_task(self._receive())

    async def send(self, pcm: bytes) -> None:
        if self.ws is None:
            return
        self._buf += pcm
        if len(self._buf) >= audio.FRAME_BYTES * 2:
            chunk, self._buf = bytes(self._buf), bytearray()
            try:
                await self.ws.send(chunk)
            except websockets.ConnectionClosed:
                self.ws = None

    async def _receive(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "Results":
                    alts = (msg.get("channel") or {}).get("alternatives") or [{}]
                    text = (alts[0].get("transcript") or "").strip()
                    if text:
                        await self.on_interim(text)
                    if msg.get("is_final") and text:
                        await self.on_final(text)
                    if msg.get("speech_final"):
                        await self.on_turn_end()
                elif kind == "UtteranceEnd":
                    await self.on_turn_end()
        except asyncio.CancelledError:
            return
        except websockets.ConnectionClosed as exc:
            log.warning("Deepgram connection closed: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("Deepgram receive failed")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self.ws is not None:
            try:
                await self.ws.send(json.dumps({"type": "CloseStream"}))
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------ text-to-speech
class TTS:
    """Turns a sentence into a stream of 8 kHz PCM chunks."""

    def __init__(self, provider: str, voice: str, model: str, settings: dict):
        self.provider = provider or "deepgram"
        self.voice = voice
        self.model = model
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))

    def check_keys(self) -> None:
        need = {"deepgram": "deepgram_api_key", "elevenlabs": "elevenlabs_api_key",
                "openai": "openai_api_key"}[self.provider]
        if not (self.settings.get(need) or "").strip():
            raise RuntimeError(f"No API key for text-to-speech provider {self.provider}")

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        if self.provider == "elevenlabs":
            gen = self._elevenlabs(text)
        elif self.provider == "openai":
            gen = self._openai(text)
        else:
            gen = self._deepgram(text)
        carry = b""
        async for chunk in gen:
            chunk = carry + chunk
            if len(chunk) % 2:
                chunk, carry = chunk[:-1], chunk[-1:]
            else:
                carry = b""
            if chunk:
                yield chunk

    async def _deepgram(self, text: str) -> AsyncIterator[bytes]:
        params = {"model": self.voice or "aura-2-thalia-en", "encoding": "linear16",
                  "sample_rate": audio.SAMPLE_RATE, "container": "none"}
        async with self.client.stream(
                "POST", "https://api.deepgram.com/v1/speak", params=params,
                headers={"Authorization": f"Token {self.settings['deepgram_api_key']}"},
                json={"text": text}) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"Deepgram TTS {r.status_code}: {(await r.aread())[:200]!r}")
            async for chunk in r.aiter_bytes(1600):
                yield chunk

    async def _elevenlabs(self, text: str) -> AsyncIterator[bytes]:
        voice = self.voice or "21m00Tcm4TlvDq8ikWAM"
        async with self.client.stream(
                "POST", f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream",
                params={"output_format": "ulaw_8000"},
                headers={"xi-api-key": self.settings["elevenlabs_api_key"]},
                json={"text": text, "model_id": self.model or "eleven_flash_v2_5"}) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"ElevenLabs {r.status_code}: {(await r.aread())[:200]!r}")
            async for chunk in r.aiter_bytes(800):
                yield audio.ulaw_to_pcm16(chunk)

    async def _openai(self, text: str) -> AsyncIterator[bytes]:
        async with self.client.stream(
                "POST", "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {self.settings['openai_api_key']}"},
                json={"model": self.model or "gpt-4o-mini-tts", "voice": self.voice or "coral",
                      "input": text, "response_format": "pcm"}) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"OpenAI TTS {r.status_code}: {(await r.aread())[:200]!r}")
            carry = b""
            async for chunk in r.aiter_bytes(4800):
                chunk = carry + chunk
                usable = len(chunk) - len(chunk) % 6  # whole samples, multiple of 3
                chunk, carry = chunk[:usable], chunk[usable:]
                if chunk:
                    yield audio.resample_pcm16(chunk, 24000, audio.SAMPLE_RATE)

    async def close(self) -> None:
        await self.client.aclose()
