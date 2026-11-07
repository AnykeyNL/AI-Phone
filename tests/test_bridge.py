"""End-to-end bridge tests: a fake Asterisk AudioSocket client talks to the
real CallManager, with a fake OpenAI Realtime server for the AI side."""

import asyncio
import base64
import json
import struct
import uuid as uuidlib

import pytest
import websockets

from aiphone import asterisk, audio
from aiphone.audiosocket import KIND_AUDIO, KIND_HANGUP, KIND_UUID
from aiphone.db import Database
from aiphone.manager import CallManager


def frame(kind, payload=b""):
    return bytes([kind]) + struct.pack(">H", len(payload)) + payload


async def read_frame(reader):
    h = await reader.readexactly(3)
    n = struct.unpack(">H", h[1:])[0]
    return h[0], (await reader.readexactly(n) if n else b"")


@pytest.fixture
def manager(tmp_path, monkeypatch, unused_port):
    monkeypatch.setattr(asterisk, "AUDIOSOCKET_PORT", unused_port)
    db = Database(tmp_path / "test.db")
    db.set("silence_timeout", 0)
    return CallManager(db)


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def unused_port():
    return free_port()


async def connect_call(manager, uid):
    reader, writer = await asyncio.open_connection("127.0.0.1", asterisk.AUDIOSOCKET_PORT)
    writer.write(frame(KIND_UUID, uuidlib.UUID(uid).bytes))
    await writer.drain()
    return reader, writer


def test_echo_call_round_trip(manager):
    async def run():
        await manager.start()
        manager.db.save_profile(None, "Echo", "700", "echo", True, {"delay_ms": 0})
        uid = manager.new_inbound("700", "line1")
        assert uid
        reader, writer = await connect_call(manager, uid)
        tone = (b"\x10\x27" * audio.FRAME_SAMPLES)
        for _ in range(10):
            writer.write(frame(KIND_AUDIO, tone))
        await writer.drain()
        got = []
        while len(got) < 10:
            kind, payload = await asyncio.wait_for(read_frame(reader), 3)
            if kind == KIND_AUDIO:
                got.append(payload)
        assert all(len(p) == audio.FRAME_BYTES for p in got)
        assert got[0] == tone
        writer.write(frame(KIND_HANGUP))
        await writer.drain()
        await asyncio.sleep(0.3)
        assert manager.db.call(uid)["status"] == "completed"
        await manager.stop()

    asyncio.run(run())


def test_unknown_extension_gets_no_call_id(manager):
    assert manager.new_inbound("999", "line1") is None


def test_openai_realtime_barge_in(manager, monkeypatch):
    ws_port = free_port()
    from aiphone.providers import openai_realtime

    received = []

    async def fake_openai(ws):
        appends = 0
        async for raw in ws:
            ev = json.loads(raw)
            received.append(ev)
            if ev["type"] == "response.create":
                await ws.send(json.dumps({"type": "response.created"}))
                chunk = base64.b64encode(b"\x7f" * 160).decode()
                for _ in range(100):  # 2 seconds of AI audio
                    await ws.send(json.dumps({"type": "response.output_audio.delta",
                                              "item_id": "item1", "delta": chunk}))
            elif ev["type"] == "input_audio_buffer.append":
                appends += 1
                if appends == 15:  # ~0.6 s into the call the caller starts talking
                    await ws.send(json.dumps({"type": "input_audio_buffer.speech_started"}))

    async def run():
        server = await websockets.serve(fake_openai, "127.0.0.1", ws_port)
        monkeypatch.setattr(openai_realtime, "REALTIME_URL",
                            f"ws://127.0.0.1:{ws_port}/?model={{model}}")
        manager.db.set("openai_api_key", "test")
        await manager.start()
        profile = manager.db.profile_by_extension("100")
        assert profile["provider"] == "openai_realtime"
        uid = manager.new_inbound("100", "line1")
        reader, writer = await connect_call(manager, uid)

        async def send_audio():
            for _ in range(60):
                writer.write(frame(KIND_AUDIO, b"\x00\x00" * audio.FRAME_SAMPLES))
                await writer.drain()
                await asyncio.sleep(0.02)

        sender = asyncio.create_task(send_audio())
        frames_in = 0
        try:
            while True:
                kind, _ = await asyncio.wait_for(read_frame(reader), 0.5)
                if kind == KIND_AUDIO:
                    frames_in += 1
        except asyncio.TimeoutError:
            pass
        await sender
        session_update = next(e for e in received if e["type"] == "session.update")
        assert session_update["session"]["audio"]["input"]["format"]["type"] == "audio/pcmu"
        assert session_update["session"]["tools"][0]["name"] == "end_call"
        truncates = [e for e in received if e["type"] == "conversation.item.truncate"]
        assert len(truncates) == 1
        assert truncates[0]["item_id"] == "item1"
        # Playback stopped well before the 2 s of audio the AI sent.
        assert 0 < truncates[0]["audio_end_ms"] < 1200
        assert frames_in < 70
        writer.write(frame(KIND_HANGUP))
        await writer.drain()
        await asyncio.sleep(0.3)
        await manager.stop()
        server.close()

    asyncio.run(run())


def test_claude_pipeline_turn_and_barge_in(manager, monkeypatch):
    from aiphone.providers import claude_pipeline, speech

    ws_port = free_port()
    seen_messages = []

    class FakeStream:
        def __init__(self, messages):
            seen_messages.append([dict(m) for m in messages])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        @property
        def text_stream(self):
            async def gen():
                for part in ["Hi there. ", "This is a long ", "answer. ", "Bye now."]:
                    yield part
            return gen()

        async def get_final_message(self):
            class M:
                content = []
            return M()

    class FakeAnthropic:
        def __init__(self, api_key):
            class Messages:
                def stream(self_inner, **kw):
                    return FakeStream(kw["messages"])
            self.messages = Messages()

    async def fake_tts(self, text):
        for _ in range(25):  # 0.5 s per sentence
            yield b"\x10\x00" * audio.FRAME_SAMPLES

    monkeypatch.setattr(claude_pipeline.anthropic, "AsyncAnthropic", FakeAnthropic)
    monkeypatch.setattr(speech.TTS, "stream", fake_tts)
    monkeypatch.setattr(speech.DeepgramSTT, "URL", f"ws://127.0.0.1:{ws_port}/")

    async def fake_deepgram(ws):
        n = 0
        async for raw in ws:
            if isinstance(raw, bytes):
                n += 1
                if n == 5:
                    await ws.send(json.dumps({"type": "Results", "is_final": True,
                        "speech_final": True,
                        "channel": {"alternatives": [{"transcript": "Hello Claude"}]}}))
                if n == 22:  # caller interrupts while the AI is talking
                    await ws.send(json.dumps({"type": "Results", "is_final": False,
                        "channel": {"alternatives": [{"transcript": "wait"}]}}))

    async def run():
        server = await websockets.serve(fake_deepgram, "127.0.0.1", ws_port)
        manager.db.set("anthropic_api_key", "x")
        manager.db.set("deepgram_api_key", "x")
        p = manager.db.profile_by_extension("1")
        p["config"]["greeting"] = ""
        manager.db.save_profile(p["id"], p["name"], "1", p["provider"], True, p["config"])
        uid = manager.new_inbound("1", "line1")
        await manager.start()
        reader, writer = await connect_call(manager, uid)

        async def send_audio():
            for _ in range(150):
                writer.write(frame(KIND_AUDIO, b"\x00\x00" * audio.FRAME_SAMPLES))
                await writer.drain()
                await asyncio.sleep(0.02)

        sender = asyncio.create_task(send_audio())
        frames_in = 0
        try:
            while True:
                kind, _ = await asyncio.wait_for(read_frame(reader), 1.0)
                if kind == KIND_AUDIO:
                    frames_in += 1
        except asyncio.TimeoutError:
            pass
        sender.cancel()
        assert seen_messages[0] == [{"role": "user", "content": "Hello Claude"}]
        # 3 sentences x 25 frames would be 75; the interruption cut it short.
        assert 10 < frames_in < 70, frames_in
        session = manager.sessions[uid]
        transcript = [t["text"] for t in session.transcript]
        assert transcript[0] == "Hello Claude"
        assert any("interrupted" in t for t in transcript)
        writer.write(frame(KIND_HANGUP))
        await writer.drain()
        await asyncio.sleep(0.3)
        await manager.stop()
        server.close()

    asyncio.run(run())
