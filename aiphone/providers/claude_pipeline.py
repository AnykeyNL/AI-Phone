"""Claude provider: Deepgram speech-to-text -> Claude -> text-to-speech.

Claude has no speech-to-speech API, so this provider chains three streaming
services. To keep latency low, Claude's answer is spoken sentence by sentence
while it is still being written. Barge-in works on Deepgram's partial
transcripts: as soon as the caller says something while the AI is talking,
playback stops and the unfinished answer is cancelled.
"""

from __future__ import annotations

import asyncio
import re

import anthropic

from .. import localinfo
from .base import END_CALL_DESCRIPTION, Provider
from .speech import TTS, DeepgramSTT

_SENTENCE_END = re.compile(r"([.!?]+[\"')\]]*)(\s+)")
MAX_SEARCHES = 3       # web searches per answer
MAX_CONTINUATIONS = 2  # resumed turns after stop_reason "pause_turn"


def _block_dict(block) -> dict:
    if isinstance(block, dict):
        return block
    if hasattr(block, "to_dict"):
        return block.to_dict()
    return block.model_dump(exclude_none=True)


class ClaudePipelineProvider(Provider):
    def __init__(self, call):
        super().__init__(call)
        self.history: list[dict] = []
        self._turn_parts: list[str] = []
        self._response: asyncio.Task | None = None
        self._spoken: dict[int, str] = {}
        self._sentence_id = 0
        self.stt: DeepgramSTT | None = None
        self.tts: TTS | None = None
        self.client: anthropic.AsyncAnthropic | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        c = self.config
        self.client = anthropic.AsyncAnthropic(
            api_key=self.require_key("anthropic_api_key", "Anthropic"))
        self.tts = TTS(c.get("tts_provider"), c.get("tts_voice"), c.get("tts_model"),
                       self.settings)
        self.tts.check_keys()
        self.stt = DeepgramSTT(
            self.require_key("deepgram_api_key", "Deepgram"), c.get("stt_model"),
            c.get("language"), int(c.get("endpointing_ms") or 500),
            self._on_interim, self._on_final, self._on_turn_end)
        await self.stt.start()

        opening = self.call.opening()
        if opening and opening.text:
            if opening.kind == "say":
                self.history = [{"role": "user", "content": "(The call has just connected.)"}]
                self._start(self._say(opening.text, hangup_after=opening.hangup_after))
            else:
                self.history = [{"role": "user", "content": (
                    "(The person has just answered your call. Greet them and open the "
                    "conversation.)")}]
                self._start(self._respond())

    async def close(self) -> None:
        if self._response:
            self._response.cancel()
        if self.stt:
            await self.stt.close()
        if self.tts:
            await self.tts.close()

    async def on_audio(self, pcm16_8k: bytes) -> None:
        if self.stt:
            await self.stt.send(pcm16_8k)

    # ------------------------------------------------------------- caller
    @property
    def _busy(self) -> bool:
        return (self._response is not None and not self._response.done()) or self.call.is_playing

    async def _on_interim(self, text: str) -> None:
        self.call.touch()
        min_words = max(1, int(self.config.get("barge_in_words") or 1))
        if self._busy and len(text.split()) >= min_words:
            self._interrupt()

    async def _on_final(self, text: str) -> None:
        self.call.touch()
        self._turn_parts.append(text)

    async def _on_turn_end(self) -> None:
        if not self._turn_parts:
            return
        text = " ".join(self._turn_parts).strip()
        self._turn_parts = []
        if self._busy:
            self._interrupt()
        self.call.add_transcript("user", text)
        if self.history and self.history[-1]["role"] == "user":
            self.history[-1]["content"] += " " + text
        else:
            self.history.append({"role": "user", "content": text})
        self._start(self._respond())

    def _interrupt(self) -> None:
        """Stop talking now and remember what the caller actually heard."""
        if self._response and not self._response.done():
            self._response.cancel()
        played = self.call.clear_playback()
        heard = [self._spoken[i] for i in sorted(self._spoken) if played.get(i, 0) > 0]
        self._spoken.clear()
        if heard:
            text = " ".join(heard) + " ..."
            if self.history and self.history[-1]["role"] == "assistant":
                self.history[-1]["content"] = text
            else:
                self.history.append({"role": "assistant", "content": text})
            self.call.add_transcript("assistant", text + " (interrupted)")
        self.log.info("Barge-in: AI interrupted")

    def _start(self, coro) -> None:
        if self._response and not self._response.done():
            self._response.cancel()
        self._spoken.clear()
        self._response = asyncio.create_task(coro)

    # ----------------------------------------------------------- speaking
    async def _speak_worker(self, queue: asyncio.Queue) -> None:
        while True:
            sentence = await queue.get()
            if sentence is None:
                return
            self._sentence_id += 1
            sid = self._sentence_id
            self._spoken[sid] = sentence
            try:
                async for chunk in self.tts.stream(sentence):
                    self.call.play(chunk, tag=sid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.error("Text-to-speech failed: %s", exc)
                self.call.add_transcript("system", f"Text-to-speech error: {exc}")

    async def _say(self, text: str, hangup_after: bool = False) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(text)
        await queue.put(None)
        await self._speak_worker(queue)
        self.history.append({"role": "assistant", "content": text})
        self.call.add_transcript("assistant", text)
        if hangup_after:
            await self.call.wait_playback_done()
            self.call.hangup("message delivered")

    def _tools(self) -> list[dict]:
        c = self.config
        tools: list[dict] = []
        if c.get("allow_end_call", True):
            tools.append({"name": "end_call", "description": END_CALL_DESCRIPTION,
                          "input_schema": {"type": "object", "properties": {}}})
        if self.web_search:
            # The basic web search tool: it works on every Claude model and answers
            # faster than the dynamic-filtering version, which matters on the phone.
            search = {"type": "web_search_20250305", "name": "web_search",
                      "max_uses": MAX_SEARCHES}
            loc = localinfo.search_location(self.settings)
            if loc:
                search["user_location"] = loc
            tools.append(search)
        return tools

    async def _respond(self) -> None:
        c = self.config
        queue: asyncio.Queue = asyncio.Queue()
        worker = asyncio.create_task(self._speak_worker(queue))
        tools = self._tools()
        full, buf, end_call = "", "", False
        assistant_blocks: list = []  # continued turn after pause_turn
        try:
            for _ in range(1 + MAX_CONTINUATIONS):
                messages = list(self.history)
                if assistant_blocks:
                    messages.append({"role": "assistant", "content": assistant_blocks})
                kwargs = dict(model=c.get("model") or "claude-haiku-5-5",
                              max_tokens=int(c.get("max_tokens") or 400),
                              system=self.instructions(), messages=messages)
                if tools:
                    kwargs["tools"] = tools
                async with self.client.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        etype = getattr(event, "type", "")
                        if etype == "text":
                            full += event.text
                            buf += event.text
                            buf = await self._flush_sentences(buf, queue)
                        elif (etype == "content_block_start" and getattr(
                                event.content_block, "type", "") == "server_tool_use"):
                            # Speak "let me check" now, while the search runs.
                            if buf.strip():
                                await queue.put(buf.strip())
                            buf = ""
                            if full and not full.endswith(" "):
                                full += " "
                    final = await stream.get_final_message()
                for b in final.content:
                    btype = getattr(b, "type", "")
                    if btype == "tool_use" and b.name == "end_call":
                        end_call = True
                    elif btype == "server_tool_use" and b.name == "web_search":
                        query = (getattr(b, "input", None) or {}).get("query", "")
                        self.log.info("Web search: %s", query)
                        self.call.add_transcript("system", f"Searched the web: {query}")
                if getattr(final, "stop_reason", None) != "pause_turn":
                    break
                # A long server-side search loop paused; send it back to continue.
                assistant_blocks += [_block_dict(b) for b in final.content]
            if buf.strip():
                await queue.put(buf.strip())
            await queue.put(None)
            await worker
        except asyncio.CancelledError:
            worker.cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            worker.cancel()
            self.log.exception("Claude request failed")
            self.call.add_transcript("system", f"Claude error: {exc}")
            return
        full = " ".join(full.split())
        if full:
            self.history.append({"role": "assistant", "content": full})
            self.call.add_transcript("assistant", full)
        elif end_call:
            self.history.append({"role": "assistant", "content": "Goodbye."})
        if end_call:
            self.call.add_transcript("system", "AI ended the call")
            await self.call.wait_playback_done()
            self.call.hangup("AI ended the call")

    @staticmethod
    async def _flush_sentences(buf: str, queue: asyncio.Queue) -> str:
        while True:
            m = _SENTENCE_END.search(buf)
            if not m:
                break
            sentence, buf = buf[:m.end(1)].strip(), buf[m.end():]
            if sentence:
                await queue.put(sentence)
        if len(buf) > 160 and "," in buf:  # long clause: speak up to the last comma
            cut = buf.rfind(",") + 1
            await queue.put(buf[:cut].strip())
            buf = buf[cut:]
        return buf
