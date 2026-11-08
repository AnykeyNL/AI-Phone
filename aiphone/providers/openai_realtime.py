"""OpenAI Realtime API provider (speech-to-speech, full duplex with barge-in).

Audio goes both ways as G.711 mu-law at 8 kHz, which is what the phone line
carries anyway, so no resampling is needed. OpenAI's voice activity
detection notices when the caller starts talking; we then drop the AI audio
that has not been played yet and tell the model where it was cut off
(``conversation.item.truncate``) so it knows what the caller actually heard.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import websockets

from .. import audio, localinfo
from .base import END_CALL_DESCRIPTION, Provider

REALTIME_URL = "wss://api.openai.com/v1/realtime?model={model}"
RESPONSES_URL = "https://api.openai.com/v1/responses"
SEND_EVERY_FRAMES = 2  # 40 ms of audio per append event
SEARCH_TIMEOUT = 25

WEB_SEARCH_DESCRIPTION = (
    "Look up live or recent information on the web: weather and forecasts, news, sports "
    "results, opening hours, prices, travel information. Write the question as a complete "
    "search query including the place and day it is about."
)


class OpenAIRealtimeProvider(Provider):
    def __init__(self, call):
        super().__init__(call)
        self.ws = None
        self._recv_task: asyncio.Task | None = None
        self._buf = bytearray()
        self._frames = 0
        self._current_item: str | None = None
        self._end_requested = False
        self._hangup_after_response = False
        self._response_active = False
        self._idle = asyncio.Event()
        self._idle.set()
        self._searches: set[asyncio.Task] = set()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        key = self.require_key("openai_api_key", "OpenAI")
        model = self.config.get("model") or "gpt-realtime"
        self.ws = await websockets.connect(
            REALTIME_URL.format(model=model),
            additional_headers={"Authorization": f"Bearer {key}"},
            max_size=None, ping_interval=20, open_timeout=15)
        await self._send({"type": "session.update", "session": self._session_config(model)})
        self._recv_task = asyncio.create_task(self._receive_loop())

        opening = self.call.opening()
        if opening and opening.text:
            if opening.kind == "say":
                prompt = ("Say exactly the following words, in the language they are "
                          f"written in, and nothing else: {opening.text}")
            else:
                prompt = ("The person has just answered your call. Greet them and open the "
                          f"conversation. Reason for calling: {opening.text}")
            self._hangup_after_response = opening.hangup_after
            await self._send({"type": "response.create", "response": {"instructions": prompt}})

    def _session_config(self, model: str) -> dict:
        c = self.config
        if c.get("turn_detection") == "server_vad":
            turn = {"type": "server_vad",
                    "threshold": float(c.get("vad_threshold") or 0.6),
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": int(c.get("silence_ms") or 600)}
        else:
            turn = {"type": "semantic_vad", "eagerness": c.get("eagerness") or "auto"}
        turn.update({"create_response": True, "interrupt_response": True})
        audio_in: dict = {"format": {"type": "audio/pcmu"}, "turn_detection": turn}
        if c.get("noise_reduction") and c["noise_reduction"] != "off":
            audio_in["noise_reduction"] = {"type": c["noise_reduction"]}
        if c.get("transcription_model"):
            audio_in["transcription"] = {"model": c["transcription_model"]}
        session = {
            "type": "realtime",
            "model": model,
            "output_modalities": ["audio"],
            "instructions": self.instructions(),
            "audio": {
                "input": audio_in,
                "output": {"format": {"type": "audio/pcmu"},
                           "voice": c.get("voice") or "marin",
                           "speed": float(c.get("speed") or 1.0)},
            },
        }
        tools = []
        if c.get("allow_end_call", True):
            tools.append({
                "type": "function", "name": "end_call", "description": END_CALL_DESCRIPTION,
                "parameters": {"type": "object", "properties": {}, "required": []},
            })
        if self.web_search:
            tools.append({
                "type": "function", "name": "web_search", "description": WEB_SEARCH_DESCRIPTION,
                "parameters": {"type": "object", "required": ["query"], "properties": {
                    "query": {"type": "string", "description": "What to look up"}}},
            })
        if tools:
            session["tools"] = tools
            session["tool_choice"] = "auto"
        return session

    async def close(self) -> None:
        if self._recv_task:
            self._recv_task.cancel()
        for task in self._searches:
            task.cancel()
        if self.ws is not None:
            await self.ws.close()

    # ---------------------------------------------------------------- audio
    async def on_audio(self, pcm16_8k: bytes) -> None:
        if self.ws is None:
            return
        self._buf += audio.pcm16_to_ulaw(pcm16_8k)
        self._frames += 1
        if self._frames >= SEND_EVERY_FRAMES:
            chunk, self._buf, self._frames = bytes(self._buf), bytearray(), 0
            try:
                await self._send({"type": "input_audio_buffer.append",
                                  "audio": base64.b64encode(chunk).decode()})
            except websockets.ConnectionClosed:
                pass

    async def _send(self, event: dict) -> None:
        await self.ws.send(json.dumps(event))

    # --------------------------------------------------------------- events
    async def _receive_loop(self) -> None:
        try:
            async for raw in self.ws:
                await self._handle(json.loads(raw))
        except asyncio.CancelledError:
            return
        except websockets.ConnectionClosed as exc:
            self.log.warning("Realtime connection closed: %s", exc)
            self.call.hangup("error: OpenAI connection closed")
            return
        except Exception as exc:  # noqa: BLE001
            self.log.exception("Realtime receive loop failed")
            self.call.hangup(f"error: {exc}")

    async def _handle(self, ev: dict) -> None:
        t = ev.get("type", "")
        if t in ("response.output_audio.delta", "response.audio.delta"):
            item = ev.get("item_id")
            self._current_item = item
            self.call.play(audio.ulaw_to_pcm16(base64.b64decode(ev["delta"])), tag=item)
        elif t == "input_audio_buffer.speech_started":
            self.call.touch()
            await self._barge_in()
        elif t == "input_audio_buffer.speech_stopped":
            self.call.touch()
        elif t == "conversation.item.input_audio_transcription.completed":
            self.call.add_transcript("user", ev.get("transcript", ""))
        elif t in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
            self.call.add_transcript("assistant", ev.get("transcript", ""))
        elif t == "response.created":
            self._response_active = True
            self._idle.clear()
        elif t == "response.function_call_arguments.done":
            if ev.get("name") == "web_search":
                try:
                    query = json.loads(ev.get("arguments") or "{}").get("query", "")
                except ValueError:
                    query = ""
                task = asyncio.create_task(self._web_search(ev.get("call_id"), query))
                self._searches.add(task)
                task.add_done_callback(self._searches.discard)
            elif ev.get("name") == "end_call":
                self._end_requested = True
                await self._send({"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": ev.get("call_id"),
                    "output": json.dumps({"ok": True})}})
        elif t == "response.done":
            self._response_active = False
            self._idle.set()
            status = (ev.get("response") or {}).get("status")
            if self._end_requested:
                self.call.add_transcript("system", "AI ended the call")
                self.call.hangup("AI ended the call", after_playback=True)
            elif self._hangup_after_response and status == "completed":
                self._hangup_after_response = False
                self.call.hangup("message delivered", after_playback=True)
        elif t == "error":
            err = ev.get("error") or {}
            code = err.get("code", "")
            if code in ("response_cancel_not_active", "item_truncate_invalid_item_id"):
                return
            self.log.error("OpenAI error: %s", err)
            self.call.add_transcript("system", f"OpenAI error: {err.get('message', err)}")
            if err.get("type") in ("invalid_request_error",) and code in (
                    "invalid_api_key", "model_not_found", "unknown_parameter"):
                self.call.hangup(f"error: {err.get('message')}", after_playback=False)

    async def _barge_in(self) -> None:
        """The caller started talking: stop the AI immediately."""
        if not self.call.is_playing:
            return
        item = self._current_item
        played = self.call.clear_playback()
        if self._hangup_after_response:
            self._hangup_after_response = False  # caller wants to reply to the message
        if item and item in played:
            await self._send({"type": "conversation.item.truncate", "item_id": item,
                              "content_index": 0, "audio_end_ms": played[item]})
        self.log.info("Barge-in: AI interrupted after %s ms", played.get(item, 0))

    # ----------------------------------------------------------- web search
    async def _web_search(self, call_id: str, query: str) -> None:
        """Answer the model's web_search call with OpenAI's web search tool."""
        self.log.info("Web search: %s", query)
        self.call.add_transcript("system", f"Searched the web: {query}")
        try:
            output = await self.search(query)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Web search failed: %s", exc)
            output = f"The search failed ({exc}). Tell the caller you couldn't look it up."
        try:
            await self._send({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": call_id, "output": output}})
            # Only one response can run at a time: wait for "let me check" to finish.
            await asyncio.wait_for(self._idle.wait(), 30)
            if not self._end_requested:
                await self._send({"type": "response.create"})
        except (websockets.ConnectionClosed, asyncio.TimeoutError):
            pass

    async def search(self, query: str) -> str:
        tool: dict = {"type": "web_search"}
        loc = localinfo.search_location(self.settings)
        if loc:
            tool["user_location"] = loc
        body = {
            "model": self.config.get("search_model") or "gpt-4.1-mini",
            "tools": [tool],
            "instructions": (
                f"Current date and time: {localinfo.now_text()}. "
                + (f"The person asking is in {localinfo.location_text(self.settings)}. "
                   if localinfo.location_text(self.settings) else "")
                + "Search the web and answer in at most four short plain-text sentences "
                "with the concrete facts and numbers; no links, lists or markdown."),
            "input": query,
        }
        key = self.require_key("openai_api_key", "OpenAI")
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
            r = await client.post(RESPONSES_URL, json=body,
                                  headers={"Authorization": f"Bearer {key}"})
            if r.status_code >= 400:
                try:
                    msg = r.json()["error"]["message"]
                except (ValueError, KeyError, TypeError):
                    msg = r.text[:200]
                raise RuntimeError(f"OpenAI {r.status_code}: {msg}")
            data = r.json()
        texts = [part.get("text", "") for item in data.get("output", [])
                 if item.get("type") == "message"
                 for part in item.get("content", []) if part.get("type") == "output_text"]
        return " ".join(t.strip() for t in texts if t.strip()) or "No results found."
