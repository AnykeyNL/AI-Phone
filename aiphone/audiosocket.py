"""Asterisk AudioSocket wire protocol.

Each frame is: 1 byte kind, 2 bytes big-endian payload length, payload.
See https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/
"""

from __future__ import annotations

import asyncio
import struct
import uuid as uuidlib

KIND_HANGUP = 0x00
KIND_UUID = 0x01
KIND_DTMF = 0x03
KIND_AUDIO = 0x10  # signed linear 16-bit, 8 kHz, mono
KIND_ERROR = 0xFF


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(3)
    kind, length = header[0], struct.unpack(">H", header[1:3])[0]
    payload = await reader.readexactly(length) if length else b""
    return kind, payload


async def write_frame(writer: asyncio.StreamWriter, kind: int, payload: bytes) -> None:
    writer.write(bytes([kind]) + struct.pack(">H", len(payload)) + payload)
    await writer.drain()


async def read_uuid(reader: asyncio.StreamReader, timeout: float = 5.0) -> str:
    kind, payload = await asyncio.wait_for(read_frame(reader), timeout)
    if kind != KIND_UUID or len(payload) != 16:
        raise ValueError(f"expected UUID frame, got kind={kind:#x} len={len(payload)}")
    return str(uuidlib.UUID(bytes=payload))
