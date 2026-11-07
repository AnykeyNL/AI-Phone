"""Audio helpers: G.711 mu-law, resampling and level detection.

Asterisk's AudioSocket delivers signed 16-bit little-endian PCM at 8 kHz,
mono, in 20 ms frames (320 bytes). Everything in this module works on raw
``bytes`` in that format unless stated otherwise.
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 160
FRAME_BYTES = FRAME_SAMPLES * 2  # 320

_BIAS = 0x84
_CLIP = 32635


def _build_ulaw_tables() -> tuple[np.ndarray, np.ndarray]:
    # Decode table: 256 mu-law codes -> int16
    dec = np.zeros(256, dtype=np.int16)
    for i in range(256):
        u = ~i & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        sample = ((mantissa << 3) + _BIAS) << exponent
        sample -= _BIAS
        dec[i] = -sample if sign else sample
    # Encode table: 65536 int16 values (offset by 32768) -> mu-law code
    pcm = np.arange(-32768, 32768, dtype=np.int32)
    sign = np.where(pcm < 0, 0x80, 0)
    mag = np.minimum(np.abs(pcm), _CLIP) + _BIAS
    exponent = np.floor(np.log2(mag)).astype(np.int32) - 7
    exponent = np.clip(exponent, 0, 7)
    mantissa = (mag >> (exponent + 3)) & 0x0F
    enc = (~(sign | (exponent << 4) | mantissa)) & 0xFF
    return dec, enc.astype(np.uint8)


_ULAW_DEC, _ULAW_ENC = _build_ulaw_tables()


def ulaw_to_pcm16(data: bytes) -> bytes:
    codes = np.frombuffer(data, dtype=np.uint8)
    return _ULAW_DEC[codes].astype("<i2").tobytes()


def pcm16_to_ulaw(data: bytes) -> bytes:
    samples = np.frombuffer(data, dtype="<i2").astype(np.int32) + 32768
    return _ULAW_ENC[samples].tobytes()


def _lowpass(x: np.ndarray, cutoff: float, taps: int = 63) -> np.ndarray:
    """Windowed-sinc low-pass filter; ``cutoff`` is a fraction of Nyquist."""
    n = np.arange(taps) - (taps - 1) / 2
    h = cutoff * np.sinc(cutoff * n) * np.hamming(taps)
    h /= h.sum()
    return np.convolve(x, h, mode="same")


def resample_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample mono int16 PCM. Good enough for telephone-band speech."""
    if src_rate == dst_rate or not data:
        return data
    x = np.frombuffer(data, dtype="<i2").astype(np.float32)
    if dst_rate < src_rate:
        x = _lowpass(x, dst_rate / src_rate)
    n_out = int(round(len(x) * dst_rate / src_rate))
    if n_out <= 0:
        return b""
    t_out = np.arange(n_out) * (src_rate / dst_rate)
    y = np.interp(t_out, np.arange(len(x)), x)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


def rms(data: bytes) -> float:
    if not data:
        return 0.0
    x = np.frombuffer(data, dtype="<i2").astype(np.float32)
    return float(np.sqrt(np.mean(x * x)))


def apply_gain(data: bytes, gain_db: float) -> bytes:
    if not gain_db:
        return data
    x = np.frombuffer(data, dtype="<i2").astype(np.float32)
    x *= 10 ** (gain_db / 20)
    return np.clip(x, -32768, 32767).astype("<i2").tobytes()


def silence(ms: int) -> bytes:
    return b"\x00\x00" * (SAMPLE_RATE * ms // 1000)
