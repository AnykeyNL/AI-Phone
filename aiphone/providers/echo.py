"""Echo test provider: plays the caller's voice back after a short delay.

Useful to check the phone, ATA and Asterisk chain without any API key.
"""

from __future__ import annotations

from .. import audio
from .base import Provider


class EchoProvider(Provider):
    async def start(self) -> None:
        delay = int(self.config.get("delay_ms") or 0)
        if delay:
            self.call.play(audio.silence(delay))
        self.call.add_transcript("system", "Echo test started")

    async def on_audio(self, pcm16_8k: bytes) -> None:
        self.call.touch()
        self.call.play(pcm16_8k)
