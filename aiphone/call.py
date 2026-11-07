"""One phone call: AudioSocket I/O, paced playback, barge-in and timeouts.

A ``CallSession`` owns the TCP connection from Asterisk and one AI provider.
The provider receives caller audio through ``on_audio`` and sends AI audio
back with ``call.play()``. When the caller interrupts, the provider calls
``call.clear_playback()``, which drops everything that has not been played
yet so the AI goes quiet within one 20 ms frame.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import audio
from .audiosocket import KIND_AUDIO, KIND_DTMF, KIND_ERROR, KIND_HANGUP, read_frame, write_frame

if TYPE_CHECKING:
    from .manager import CallManager

log = logging.getLogger("aiphone.call")


@dataclass
class Opening:
    """What the AI says first. ``kind`` is 'say' (speak text verbatim) or
    'prompt' (text is an instruction; the AI writes its own opening line)."""

    kind: str
    text: str
    hangup_after: bool = False


@dataclass
class CallInfo:
    uuid: str
    direction: str  # inbound | outbound
    profile: dict[str, Any]
    line: str = ""
    request: dict[str, Any] | None = None
    started: float = field(default_factory=time.time)


class CallSession:
    def __init__(self, manager: "CallManager", info: CallInfo,
                 reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.manager = manager
        self.db = manager.db
        self.info = info
        self.reader = reader
        self.writer = writer
        self.provider = None
        self.transcript: list[dict[str, Any]] = []
        self._chunks: deque[tuple[bytes, Any]] = deque()  # (pcm, tag)
        self._partial = b""
        self._partial_tag: Any = None
        self._played_by_tag: dict[Any, int] = {}  # tag -> samples played
        self._playback_idle = asyncio.Event()
        self._playback_idle.set()
        self._closed = asyncio.Event()
        self._hangup_when_idle = False
        self._hangup_reason = ""
        self.last_activity = time.monotonic()
        self.connected_at = time.monotonic()
        self._tasks: list[asyncio.Task] = []
        settings = self.db.all_settings()
        self.max_seconds = int(settings.get("max_call_seconds") or 1800)
        self.silence_timeout = int(settings.get("silence_timeout") or 0)
        self.log_transcripts = bool(settings.get("log_transcripts", True))
        self.output_gain_db = float(info.profile["config"].get("output_gain_db", 0) or 0)

    # ------------------------------------------------------------ public API
    @property
    def config(self) -> dict[str, Any]:
        return self.info.profile["config"]

    @property
    def settings(self) -> dict[str, Any]:
        return self.db.all_settings()

    def opening(self) -> Opening | None:
        req = self.info.request
        if req:
            text = (req.get("text") or "").strip()
            if req.get("mode") == "message":
                return Opening("say", text, hangup_after=not req.get("allow_reply", False))
            return Opening("prompt", text or "Start a friendly conversation.")
        greeting = (self.config.get("greeting") or "").strip()
        return Opening("say", greeting) if greeting else None

    def play(self, pcm16_8k: bytes, tag: Any = None) -> None:
        """Queue 8 kHz PCM for playback to the phone."""
        if not pcm16_8k or self._closed.is_set():
            return
        if self.output_gain_db:
            pcm16_8k = audio.apply_gain(pcm16_8k, self.output_gain_db)
        self._chunks.append((pcm16_8k, tag))
        self._playback_idle.clear()

    def clear_playback(self) -> dict[Any, int]:
        """Drop unplayed audio. Returns milliseconds played per tag."""
        self._chunks.clear()
        self._partial = b""
        self._playback_idle.set()
        return self.played_ms()

    def played_ms(self) -> dict[Any, int]:
        return {t: s * 1000 // audio.SAMPLE_RATE for t, s in self._played_by_tag.items()}

    @property
    def is_playing(self) -> bool:
        return not self._playback_idle.is_set()

    async def wait_playback_done(self) -> None:
        await self._playback_idle.wait()

    def add_transcript(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.transcript.append({"t": round(time.time() - self.info.started, 1),
                                "role": role, "text": text})
        log.info("[%s] %s: %s", self.info.uuid[:8], role, text)
        if self.log_transcripts:
            self.db.update_call(self.info.uuid, transcript=self.transcript)
        self.manager.notify()

    def touch(self) -> None:
        """Mark that the caller is active (resets the silence timer)."""
        self.last_activity = time.monotonic()

    def hangup(self, reason: str = "hangup", after_playback: bool = True) -> None:
        self._hangup_reason = self._hangup_reason or reason
        if after_playback and self.is_playing:
            self._hangup_when_idle = True
        else:
            self._closed.set()

    # ------------------------------------------------------------- internals
    async def run(self) -> None:
        from .providers import create_provider

        log.info("Call %s started (%s, profile=%s, line=%s)", self.info.uuid,
                 self.info.direction, self.info.profile["name"], self.info.line)
        self.db.update_call(self.info.uuid, status="active", answered=time.time())
        self.manager.notify()
        try:
            self.provider = create_provider(self)
            self._tasks = [
                asyncio.create_task(self._reader_loop(), name="reader"),
                asyncio.create_task(self._playout_loop(), name="playout"),
                asyncio.create_task(self._watchdog(), name="watchdog"),
            ]
            await self.provider.start()
            await self._closed.wait()
        except Exception as exc:  # noqa: BLE001
            log.exception("Call %s failed", self.info.uuid)
            self._hangup_reason = self._hangup_reason or f"error: {exc}"
            self.add_transcript("system", f"Error: {exc}")
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        self._closed.set()
        if self.provider is not None:
            try:
                await asyncio.wait_for(self.provider.close(), 5)
            except Exception:  # noqa: BLE001
                log.exception("Provider close failed")
        for t in self._tasks:
            t.cancel()
        try:
            await write_frame(self.writer, KIND_HANGUP, b"")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), 2)
        except Exception:  # noqa: BLE001
            pass
        reason = self._hangup_reason or "caller hung up"
        status = "failed" if reason.startswith("error") else "completed"
        self.db.update_call(self.info.uuid, status=status, reason=reason,
                            ended=time.time(), transcript=self.transcript)
        log.info("Call %s ended: %s", self.info.uuid, reason)
        self.manager.notify()

    async def _reader_loop(self) -> None:
        try:
            while not self._closed.is_set():
                kind, payload = await read_frame(self.reader)
                if kind == KIND_AUDIO:
                    if self.provider is not None:
                        await self.provider.on_audio(payload)
                elif kind == KIND_DTMF:
                    digit = payload.decode(errors="ignore")
                    log.info("DTMF %s", digit)
                    if self.provider is not None:
                        await self.provider.on_dtmf(digit)
                elif kind == KIND_HANGUP:
                    self._hangup_reason = self._hangup_reason or "caller hung up"
                    break
                elif kind == KIND_ERROR:
                    log.warning("AudioSocket error frame: %r", payload)
        except (asyncio.IncompleteReadError, ConnectionError):
            self._hangup_reason = self._hangup_reason or "caller hung up"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Reader loop failed")
        self._closed.set()

    def _next_frame(self) -> bytes | None:
        need = audio.FRAME_BYTES
        buf = self._partial
        tag = self._partial_tag
        while len(buf) < need and self._chunks:
            chunk, tag = self._chunks.popleft()
            buf += chunk
            self._partial_tag = tag
        if not buf:
            return None
        frame, self._partial = buf[:need], buf[need:]
        if len(frame) < need:
            frame += b"\x00" * (need - len(frame))
        self._played_by_tag[tag] = self._played_by_tag.get(tag, 0) + audio.FRAME_SAMPLES
        return frame

    async def _playout_loop(self) -> None:
        """Send one 20 ms frame per tick, paced to real time.

        Asterisk plays whatever it receives immediately, so sending audio
        faster than real time would build a buffer we can no longer cancel
        on barge-in. Pacing keeps at most ~2 frames in flight.
        """
        interval = audio.FRAME_MS / 1000
        next_tick = time.monotonic()
        while not self._closed.is_set():
            frame = self._next_frame()
            if frame is None:
                if not self._playback_idle.is_set():
                    self.touch()  # silence timer starts when the AI stops talking
                self._playback_idle.set()
                if self._hangup_when_idle:
                    await asyncio.sleep(0.3)
                    self._closed.set()
                    return
                await asyncio.sleep(interval)
                next_tick = time.monotonic()
                continue
            try:
                await write_frame(self.writer, KIND_AUDIO, frame)
            except (ConnectionError, RuntimeError):
                self._closed.set()
                return
            next_tick += interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.2:
                next_tick = time.monotonic()

    async def _watchdog(self) -> None:
        while not self._closed.is_set():
            await asyncio.sleep(1)
            now = time.monotonic()
            if self.max_seconds and now - self.connected_at > self.max_seconds:
                log.info("Max call duration reached")
                self.hangup("max duration reached", after_playback=False)
            elif (self.silence_timeout and not self.is_playing
                  and now - self.last_activity > self.silence_timeout):
                log.info("Silence timeout reached")
                self.hangup("silence timeout", after_playback=False)
