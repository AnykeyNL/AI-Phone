"""AI providers. Each profile (an extension you can dial) uses one provider.

``PROVIDERS`` describes every provider's settings so the web interface can
render a form for it without provider-specific templates.
"""

from __future__ import annotations

from typing import Any

COMMON_FIELDS: list[dict[str, Any]] = [
    {"key": "instructions", "label": "System prompt", "type": "textarea", "rows": 8,
     "default": (
         "You are a friendly assistant that people talk to through an old-fashioned "
         "telephone. Keep answers short and conversational, like a real phone call: "
         "one to three sentences unless the caller asks for more. Never use lists, "
         "markdown or emoji; everything you write is spoken aloud. If the caller says "
         "goodbye or wants to end the call, say a short goodbye and use the end_call tool."
     ),
     "help": "How the AI should behave. Keep it short and phone-friendly."},
    {"key": "greeting", "label": "Greeting", "type": "text",
     "default": "Hello, you're speaking with your AI assistant. How can I help?",
     "help": "Spoken as soon as the phone connects. Leave empty to wait for the caller."},
    {"key": "allow_end_call", "label": "Let the AI hang up", "type": "checkbox", "default": True,
     "help": "Gives the AI an end_call tool so it can hang up after saying goodbye."},
    {"key": "allow_web_search", "label": "Let the AI search the web", "type": "checkbox",
     "default": True,
     "help": ("For live information such as the weather, news and opening hours. "
              "Searches cost a little extra per call; set your location on the Settings page.")},
    {"key": "output_gain_db", "label": "Output volume (dB)", "type": "number", "default": 0,
     "step": 1, "help": "Raise or lower the AI's voice in the handset, e.g. -3 or 3."},
]

PROVIDERS: dict[str, dict[str, Any]] = {
    "openai_realtime": {
        "label": "OpenAI Realtime (speech-to-speech, full duplex)",
        "requires": ["openai_api_key"],
        "fields": [
            {"key": "model", "label": "Model", "type": "text", "default": "gpt-realtime",
             "suggestions": ["gpt-realtime", "gpt-realtime-mini"],
             "help": "gpt-realtime sounds best; gpt-realtime-mini is cheaper."},
            {"key": "voice", "label": "Voice", "type": "select", "default": "marin",
             "options": ["marin", "cedar", "alloy", "ash", "ballad", "coral", "echo",
                         "sage", "shimmer", "verse"]},
            {"key": "turn_detection", "label": "Turn detection", "type": "select",
             "default": "semantic_vad", "options": ["semantic_vad", "server_vad"],
             "help": "semantic_vad waits until you sound finished; server_vad uses silence."},
            {"key": "eagerness", "label": "Eagerness (semantic VAD)", "type": "select",
             "default": "auto", "options": ["auto", "low", "medium", "high"],
             "help": "How quickly the AI jumps in after you stop talking."},
            {"key": "silence_ms", "label": "Silence before answering (server VAD, ms)",
             "type": "number", "default": 600, "step": 50},
            {"key": "vad_threshold", "label": "Speech threshold (server VAD, 0-1)",
             "type": "number", "default": 0.6, "step": 0.05,
             "help": "Higher ignores more background noise and echo."},
            {"key": "noise_reduction", "label": "Noise reduction", "type": "select",
             "default": "near_field", "options": ["near_field", "far_field", "off"]},
            {"key": "transcription_model", "label": "Transcribe the caller with", "type": "text",
             "default": "gpt-4o-mini-transcribe",
             "help": "Only used for the call log. Leave empty to skip."},
            {"key": "speed", "label": "Speaking speed", "type": "number", "default": 1.0,
             "step": 0.05},
            {"key": "search_model", "label": "Web search model", "type": "text",
             "default": "gpt-4.1-mini", "suggestions": ["gpt-4.1-mini", "gpt-4.1", "gpt-5-mini"],
             "help": ("The Realtime model can't search by itself; this OpenAI model looks "
                      "things up for it. gpt-4.1-mini is fast and cheap.")},
        ],
    },
    "claude_pipeline": {
        "label": "Claude (speech-to-text, Claude, text-to-speech)",
        "requires": ["anthropic_api_key", "deepgram_api_key"],
        "fields": [
            {"key": "model", "label": "Claude model", "type": "text", "default": "claude-haiku-5-5",
             "suggestions": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5",
                             "claude-haiku-4-5", "claude-sonnet-4-5"],
             "help": "Haiku answers fastest; Sonnet and Opus think more but take longer."},
            {"key": "max_tokens", "label": "Max answer length (tokens)", "type": "number",
             "default": 400, "step": 50},
            {"key": "stt_model", "label": "Deepgram speech-to-text model", "type": "text",
             "default": "nova-3", "suggestions": ["nova-3", "nova-2"]},
            {"key": "language", "label": "Language (speech-to-text)", "type": "text",
             "default": "en", "suggestions": ["en", "en-GB", "nl", "de", "fr", "multi"],
             "help": "Language code for Deepgram, or 'multi' for automatic."},
            {"key": "endpointing_ms", "label": "Silence before answering (ms)", "type": "number",
             "default": 500, "step": 50},
            {"key": "barge_in_words", "label": "Words needed to interrupt the AI", "type": "number",
             "default": 1, "step": 1,
             "help": "Raise to 2 or 3 if echo or background noise interrupts the AI."},
            {"key": "tts_provider", "label": "Text-to-speech", "type": "select",
             "default": "deepgram", "options": ["deepgram", "elevenlabs", "openai"],
             "help": "Deepgram uses the same key as speech-to-text."},
            {"key": "tts_voice", "label": "Voice", "type": "text", "default": "aura-2-thalia-en",
             "help": ("Deepgram: a model such as aura-2-thalia-en or aura-2-orion-en. "
                      "ElevenLabs: a voice ID. OpenAI: coral, marin, alloy, ...")},
            {"key": "tts_model", "label": "TTS model (ElevenLabs / OpenAI)", "type": "text",
             "default": "",
             "help": "Empty = eleven_flash_v2_5 for ElevenLabs, gpt-4o-mini-tts for OpenAI."},
        ],
    },
    "echo": {
        "label": "Echo test (no AI, plays your voice back)",
        "requires": [],
        "fields": [
            {"key": "delay_ms", "label": "Delay (ms)", "type": "number", "default": 500, "step": 100},
        ],
    },
}


def provider_fields(provider: str) -> list[dict[str, Any]]:
    fields = PROVIDERS[provider]["fields"]
    if provider == "echo":
        return fields
    return COMMON_FIELDS + fields


def default_profile_config(provider: str) -> dict[str, Any]:
    return {f["key"]: f["default"] for f in provider_fields(provider)}


def create_provider(call):
    provider = call.info.profile["provider"]
    # Fill in defaults for settings added after the profile was saved.
    config = default_profile_config(provider)
    config.update(call.info.profile["config"])
    call.info.profile["config"] = config
    if provider == "openai_realtime":
        from .openai_realtime import OpenAIRealtimeProvider
        return OpenAIRealtimeProvider(call)
    if provider == "claude_pipeline":
        from .claude_pipeline import ClaudePipelineProvider
        return ClaudePipelineProvider(call)
    if provider == "echo":
        from .echo import EchoProvider
        return EchoProvider(call)
    raise ValueError(f"Unknown provider {provider!r}")
