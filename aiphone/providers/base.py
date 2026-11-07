"""Base class for AI providers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..call import CallSession

END_CALL_DESCRIPTION = (
    "Hang up the phone. Use this only after you have said goodbye, when the caller "
    "wants to end the call or the conversation is clearly finished."
)


class Provider:
    def __init__(self, call: "CallSession"):
        self.call = call
        self.config: dict[str, Any] = call.config
        self.settings: dict[str, Any] = call.settings
        self.log = logging.getLogger(f"aiphone.{type(self).__name__}")

    def require_key(self, name: str, label: str) -> str:
        key = (self.settings.get(name) or "").strip()
        if not key:
            raise RuntimeError(f"No {label} API key configured (Settings page)")
        return key

    def instructions(self) -> str:
        """System prompt, extended with outbound-call context when needed."""
        text = (self.config.get("instructions") or "").strip()
        req = self.call.info.request
        if req:
            if req.get("mode") == "message":
                text += ("\n\nYou placed this call to deliver a message, which you have "
                         "already read out. Answer follow-up questions briefly.")
            else:
                text += ("\n\nYou are placing this call; the person just picked up the phone. "
                         f"Reason for the call: {req.get('text') or 'a friendly chat'}")
        return text

    async def start(self) -> None:
        raise NotImplementedError

    async def on_audio(self, pcm16_8k: bytes) -> None:
        raise NotImplementedError

    async def on_dtmf(self, digit: str) -> None:
        return None

    async def close(self) -> None:
        return None
