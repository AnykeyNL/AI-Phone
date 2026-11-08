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


def test_trunk_and_speed_dials():
    settings = {
        "sip_port": 5060, "codecs": "ulaw", "lines": [], "echo_test_extension": "600",
        "trunk": {"enabled": True, "host": "sip.cheapconnect.net", "port": 5060,
                  "username": "31201234567", "password": "pw", "dial_timeout": 45},
        "speed_dials": [
            {"digits": "5", "name": "Mobile", "number": "06-1234 5678", "enabled": True},
            {"digits": "1", "name": "Clash", "number": "0611111111", "enabled": True},
            {"digits": "7", "name": "Off", "number": "0622222222", "enabled": False},
        ],
    }
    pjsip = asterisk.render_pjsip(settings)
    assert "server_uri=sip:sip.cheapconnect.net:5060" in pjsip
    assert "client_uri=sip:31201234567@sip.cheapconnect.net" in pjsip
    assert "context=aiphone-trunk-in" in pjsip
    profiles = [{"name": "Claude", "extension": "1", "enabled": True}]
    ext = asterisk.render_extensions(settings, profiles, 8080)
    assert "Dial(PJSIP/0612345678@trunk,45,U(aiphone-dial-answered^${AIPHONE_DIAL}))" in ext
    assert "/internal/dial/start?digits=5" in ext and "hangup_handler_push" in ext
    assert "0611111111" not in ext  # extension 1 belongs to an AI profile
    assert "0622222222" not in ext  # disabled
    # Without a password the trunk is not generated and speed dials are inactive.
    settings["trunk"]["password"] = ""
    assert "trunk" not in asterisk.render_pjsip(settings).split("[transport-udp]")[1]
    assert "@trunk" not in asterisk.render_extensions(settings, profiles, 8080)


def test_update_log_status_ignores_previous_run(tmp_path, monkeypatch):
    import os
    from aiphone import updater
    log = tmp_path / "update.log"
    monkeypatch.setattr(updater, "UPDATE_LOG", log)
    log.write_text("Update started\nUpdate finished 2026-11-01\n")
    old = time.time() - 3600
    os.utime(log, (old, old))
    # A new update was just requested; the old log must not count as finished.
    s = updater.log_status(time.time())
    assert s["waiting"] and not s["finished"] and s["log"] == ""
    log.write_text("Update started\nDownloading\n")
    s = updater.log_status(time.time() - 5)
    assert not s["waiting"] and not s["finished"] and "Downloading" in s["log"]
    log.write_text("Update started\nError: the update failed (line 9).\n")
    assert updater.log_status(time.time() - 5)["failed"]
    log.write_text("Update started\nUpdate finished\n")
    assert updater.log_status(time.time() - 5)["finished"]


def test_speed_dial_calls_are_logged(tmp_path):
    from aiphone.db import Database
    from aiphone.manager import CallManager
    db = Database(tmp_path / "t.db")
    db.set("speed_dials", [{"digits": "5", "name": "Mobile", "number": "06-1234 5678",
                            "enabled": True}])
    m = CallManager(db)
    uid = m.speed_dial_started("5", "line1")
    c = db.call(uid)
    assert c["direction"] == "speed-dial" and c["status"] == "ringing"
    assert c["profile"] == "5 → Mobile (0612345678)" and c["line"] == "line1"
    m.speed_dial_answered(uid)
    assert db.call(uid)["status"] == "active"
    m.speed_dial_ended(uid, "ANSWER", "42")
    c = db.call(uid)
    assert c["status"] == "completed" and 41 <= c["ended"] - c["answered"] <= 43
    for status, expect in (("BUSY", "failed"), ("NOANSWER", "no-answer"),
                           ("CANCEL", "no-answer"), ("CHANUNAVAIL", "failed")):
        uid = m.speed_dial_started("5", "line1")
        m.speed_dial_ended(uid, status, "", "17")
        assert db.call(uid)["status"] == expect, status
