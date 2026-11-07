import time
from datetime import datetime, timedelta

import numpy as np

from aiphone import asterisk, audio
from aiphone.scheduler import next_run


def test_ulaw_roundtrip_is_close():
    t = np.arange(800) / 8000
    pcm = (np.sin(2 * np.pi * 440 * t) * 12000).astype("<i2").tobytes()
    back = np.frombuffer(audio.ulaw_to_pcm16(audio.pcm16_to_ulaw(pcm)), dtype="<i2")
    orig = np.frombuffer(pcm, dtype="<i2")
    assert len(back) == len(orig)
    assert np.max(np.abs(back.astype(int) - orig.astype(int))) < 600


def test_ulaw_matches_known_values():
    # 0 encodes to 0xFF, full-scale negative to 0x00
    assert audio.pcm16_to_ulaw(b"\x00\x00") == b"\xff"
    assert audio.pcm16_to_ulaw(np.array([-32768], "<i2").tobytes()) == b"\x00"


def test_resample_length():
    pcm = b"\x00\x00" * 2400  # 100 ms at 24 kHz
    assert len(audio.resample_pcm16(pcm, 24000, 8000)) == 800 * 2


def test_render_pjsip_only_enabled_lines():
    settings = {"sip_port": 5060, "codecs": "ulaw,alaw", "lines": [
        {"id": "line1", "username": "line1", "password": "pw1", "enabled": True},
        {"id": "line2", "username": "line2", "password": "pw2", "enabled": False}]}
    conf = asterisk.render_pjsip(settings)
    assert "[line1]" in conf and "password=pw1" in conf
    assert "line2" not in conf


def test_render_extensions_has_profiles_and_echo():
    profiles = [{"name": "Claude", "extension": "1", "enabled": True},
                {"name": "Off", "extension": "5", "enabled": False}]
    conf = asterisk.render_extensions({"echo_test_extension": "600"}, profiles, 8080)
    assert "exten => 1,1" in conf
    assert "exten => 5,1" not in conf
    assert "exten => 600,1" in conf
    assert "AudioSocket(${AIPHONE_UUID},127.0.0.1:9092)" in conf
    assert "127.0.0.1:8080/internal/call/new" in conf


def test_next_run_daily_and_weekly():
    now = time.time()
    daily = {"kind": "daily", "at_time": datetime.fromtimestamp(now + 120).strftime("%H:%M")}
    nr = next_run(daily, after=now)
    assert nr is not None and 0 < nr - now <= 180
    tomorrow = (datetime.now() + timedelta(days=1)).weekday()
    weekly = {"kind": "weekly", "at_time": "07:30", "weekdays": str(tomorrow)}
    nr = next_run(weekly, after=now)
    assert datetime.fromtimestamp(nr).weekday() == tomorrow


def test_next_run_once_in_past_is_none():
    past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    assert next_run({"kind": "once", "at_datetime": past}) is None
    future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    assert next_run({"kind": "once", "at_datetime": future}) is not None
