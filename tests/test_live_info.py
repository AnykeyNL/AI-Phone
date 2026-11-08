"""Live information: date, time and location in the prompt, and web search
for Claude (Anthropic's server tool) and ChatGPT (a function tool)."""

import asyncio
import json
from types import SimpleNamespace

from aiphone import localinfo
from aiphone.providers import default_profile_config
from aiphone.providers.claude_pipeline import ClaudePipelineProvider
from aiphone.providers.openai_realtime import OpenAIRealtimeProvider

SETTINGS = {"location_city": "Amsterdam", "location_region": "",
            "location_country": "nl", "openai_api_key": "x"}


class FakeCall:
    def __init__(self, provider):
        self.config = default_profile_config(provider)
        self.settings = dict(SETTINGS)
        self.info = SimpleNamespace(request=None)
        self.transcript = []
        self.played = []
        self.is_playing = False

    def add_transcript(self, role, text):
        self.transcript.append((role, text))

    def play(self, pcm, tag=None):
        self.played.append(tag)

    def clear_playback(self):
        return {}

    def touch(self):
        pass

    def hangup(self, reason, after_playback=True):
        self.transcript.append(("hangup", reason))

    async def wait_playback_done(self):
        pass


def test_prompt_has_date_location_and_search_hint(monkeypatch):
    monkeypatch.setattr(localinfo, "timezone_name", lambda: "Europe/Amsterdam")
    text = localinfo.prompt_context(SETTINGS, web_search=True)
    assert "Current date and time:" in text and "(Europe/Amsterdam)" in text
    assert "Amsterdam, nl" in text
    assert "web_search" in text
    assert "web_search" not in localinfo.prompt_context(SETTINGS, web_search=False)
    assert localinfo.search_location(SETTINGS) == {
        "type": "approximate", "city": "Amsterdam", "country": "NL",
        "timezone": "Europe/Amsterdam"}
    # A country name instead of a code is kept for the prompt but not sent to the tool.
    loc = localinfo.search_location({"location_country": "Netherlands"})
    assert loc is None or "country" not in loc


def test_claude_web_search_speaks_filler_and_resumes_pause_turn():
    calls = []
    rounds = [
        # Round 1: filler, the search starts, then the server loop pauses.
        ([SimpleNamespace(type="text", text="One moment, let me check."),
          SimpleNamespace(type="content_block_start",
                          content_block=SimpleNamespace(type="server_tool_use"))],
         SimpleNamespace(stop_reason="pause_turn", content=[
             {"type": "text", "text": "One moment, let me check."},
             SimpleNamespace(type="server_tool_use", name="web_search",
                             input={"query": "weather Amsterdam today"},
                             to_dict=lambda: {"type": "server_tool_use", "id": "srv1",
                                              "name": "web_search",
                                              "input": {"query": "weather Amsterdam today"}})])),
        # Round 2: the answer.
        ([SimpleNamespace(type="text", text="It's 12 degrees "),
          SimpleNamespace(type="text", text="and cloudy.")],
         SimpleNamespace(stop_reason="end_turn", content=[])),
    ]

    class FakeStream:
        def __init__(self, kw):
            calls.append(kw)
            self.events, self.final = rounds[len(calls) - 1]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def __aiter__(self):
            for e in self.events:
                yield e

        async def get_final_message(self):
            return self.final

    spoken = []

    class FakeTTS:
        async def stream(self, text):
            spoken.append(text)
            yield b"\x00\x00"

    call = FakeCall("claude_pipeline")
    p = ClaudePipelineProvider(call)
    p.client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: FakeStream(kw)))
    p.tts = FakeTTS()
    p.history = [{"role": "user", "content": "What's the weather?"}]
    asyncio.run(p._respond())

    tools = {t["name"]: t for t in calls[0]["tools"]}
    assert tools["web_search"]["type"] == "web_search_20250305"
    assert tools["web_search"]["user_location"]["city"] == "Amsterdam"
    assert "Current date and time" in calls[0]["system"]
    # The filler is spoken on its own, before the search result arrives.
    assert spoken == ["One moment, let me check.", "It's 12 degrees and cloudy."]
    # pause_turn: the paused assistant turn is sent back, with no extra user message.
    assert calls[1]["messages"][-1]["role"] == "assistant"
    assert calls[1]["messages"][-1]["content"][1]["type"] == "server_tool_use"
    assert ("system", "Searched the web: weather Amsterdam today") in call.transcript
    assert p.history[-1] == {"role": "assistant",
                             "content": "One moment, let me check. It's 12 degrees and cloudy."}


def test_claude_without_web_search():
    call = FakeCall("claude_pipeline")
    call.config["allow_web_search"] = False
    names = [t["name"] for t in ClaudePipelineProvider(call)._tools()]
    assert names == ["end_call"]


def test_openai_realtime_web_search_function():
    call = FakeCall("openai_realtime")
    p = OpenAIRealtimeProvider(call)
    tools = [t["name"] for t in p._session_config("gpt-realtime")["tools"]]
    assert tools == ["end_call", "web_search"]

    sent, queries = [], []

    async def fake_send(ev):
        sent.append(ev)

    async def fake_search(query):
        queries.append(query)
        return "12 degrees and cloudy in Amsterdam."

    p._send = fake_send
    p.search = fake_search

    async def run():
        await p._handle({"type": "response.created"})
        await p._handle({"type": "response.function_call_arguments.done", "name": "web_search",
                         "call_id": "c1", "arguments": json.dumps({"query": "weather"})})
        await asyncio.sleep(0.05)
        # The answer is in, but "let me check" is still playing: no new response yet.
        assert [e["type"] for e in sent] == ["conversation.item.create"]
        assert sent[0]["item"] == {"type": "function_call_output", "call_id": "c1",
                                   "output": "12 degrees and cloudy in Amsterdam."}
        await p._handle({"type": "response.done", "response": {"status": "completed"}})
        await asyncio.sleep(0.05)
        assert sent[-1] == {"type": "response.create"}

    asyncio.run(run())
    assert queries == ["weather"]
    assert ("system", "Searched the web: weather") in call.transcript


def test_openai_search_request(monkeypatch):
    import httpx

    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"output": [
            {"type": "web_search_call"},
            {"type": "message", "content": [{"type": "output_text", "text": "Sunny, 18 C."}]}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    p = OpenAIRealtimeProvider(FakeCall("openai_realtime"))
    assert asyncio.run(p.search("weather Amsterdam")) == "Sunny, 18 C."
    assert seen["body"]["model"] == "gpt-4.1-mini"
    assert seen["body"]["tools"][0]["type"] == "web_search"
    assert seen["body"]["tools"][0]["user_location"]["country"] == "NL"
