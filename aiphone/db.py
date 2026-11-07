"""SQLite storage for settings, profiles, schedules and the call log.

Everything the web interface manages lives here, so no configuration file has
to be edited by hand. Asterisk configuration files are *generated* from this
data (see ``asterisk.py``).
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("AIPHONE_DATA_DIR", "/var/lib/aiphone"))
DB_PATH = DATA_DIR / "aiphone.db"
BOOTSTRAP_PATH = DATA_DIR / "bootstrap.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    extension TEXT NOT NULL UNIQUE,
    provider  TEXT NOT NULL,
    enabled   INTEGER NOT NULL DEFAULT 1,
    config    TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS schedules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    kind        TEXT NOT NULL,           -- once | daily | weekly
    at_datetime TEXT,                    -- once: 'YYYY-MM-DDTHH:MM' local time
    at_time     TEXT,                    -- daily/weekly: 'HH:MM' local time
    weekdays    TEXT,                    -- weekly: '0,1,2' (0 = Monday)
    call        TEXT NOT NULL,           -- JSON outbound call request
    last_run    REAL,
    next_run    REAL
);
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid        TEXT NOT NULL UNIQUE,
    direction   TEXT NOT NULL,           -- inbound | outbound
    line        TEXT,
    profile_id  INTEGER,
    profile     TEXT,
    provider    TEXT,
    status      TEXT NOT NULL,           -- ringing | active | completed | no-answer | failed
    reason      TEXT,
    started     REAL NOT NULL,
    answered    REAL,
    ended       REAL,
    request     TEXT,                    -- JSON outbound request (outbound only)
    transcript  TEXT NOT NULL DEFAULT '[]'
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "openai_api_key": "",
    "anthropic_api_key": "",
    "deepgram_api_key": "",
    "elevenlabs_api_key": "",
    "server_ip": "",
    "sip_port": 5060,
    "rtp_start": 10000,
    "rtp_end": 10200,
    "codecs": "ulaw,alaw",
    "lines": [],
    "default_extension": "100",
    "echo_test_extension": "600",
    "autodial_delay": 3,
    "max_call_seconds": 1800,
    "silence_timeout": 90,
    "ring_timeout": 40,
    "outbound_caller_name": "AI Phone",
    "outbound_caller_number": "999",
    "webhook_token": "",
    "admin_password_hash": "",
    "session_secret": "",
    "log_transcripts": True,
    "keep_calls_days": 90,
}


def _default_lines() -> list[dict[str, Any]]:
    return [
        {
            "id": f"line{i}",
            "label": f"Phone port {i}",
            "username": f"line{i}",
            "password": secrets.token_urlsafe(12),
            "enabled": i == 1,
        }
        for i in (1, 2)
    ]


class Database:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._init_defaults()

    # ------------------------------------------------------------------ basics
    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # --------------------------------------------------------------- settings
    def _init_defaults(self) -> None:
        existing = {r["key"] for r in self.query("SELECT key FROM settings")}
        for key, value in DEFAULT_SETTINGS.items():
            if key not in existing:
                self.set(key, value)
        if not self.get("lines"):
            self.set("lines", _default_lines())
        if not self.get("webhook_token"):
            self.set("webhook_token", secrets.token_urlsafe(24))
        if not self.get("session_secret"):
            self.set("session_secret", secrets.token_urlsafe(32))
        if not self.query("SELECT id FROM profiles LIMIT 1"):
            self._create_default_profiles()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return DEFAULT_SETTINGS.get(key, default)
        return json.loads(row["value"])

    def set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def all_settings(self) -> dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        for r in self.query("SELECT key, value FROM settings"):
            out[r["key"]] = json.loads(r["value"])
        return out

    def bootstrap(self) -> dict[str, Any]:
        """Values written once by the installer (AMI credentials, web port)."""
        try:
            return json.loads(BOOTSTRAP_PATH.read_text())
        except (OSError, ValueError):
            return {}

    # --------------------------------------------------------------- profiles
    def _create_default_profiles(self) -> None:
        from .providers import default_profile_config

        self.save_profile(None, "ChatGPT (default)", "100", "openai_realtime",
                          True, default_profile_config("openai_realtime"))
        self.save_profile(None, "Claude", "1", "claude_pipeline",
                          True, default_profile_config("claude_pipeline"))
        self.save_profile(None, "ChatGPT", "2", "openai_realtime",
                          True, default_profile_config("openai_realtime"))

    def profiles(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM profiles ORDER BY extension")
        for r in rows:
            r["config"] = json.loads(r["config"])
            r["enabled"] = bool(r["enabled"])
        return rows

    def profile(self, profile_id: int) -> dict[str, Any] | None:
        r = self.query_one("SELECT * FROM profiles WHERE id = ?", (profile_id,))
        if r:
            r["config"] = json.loads(r["config"])
            r["enabled"] = bool(r["enabled"])
        return r

    def profile_by_extension(self, ext: str) -> dict[str, Any] | None:
        r = self.query_one("SELECT id FROM profiles WHERE extension = ?", (ext,))
        return self.profile(r["id"]) if r else None

    def save_profile(self, profile_id: int | None, name: str, extension: str,
                     provider: str, enabled: bool, config: dict[str, Any]) -> int:
        if profile_id is None:
            cur = self.execute(
                "INSERT INTO profiles(name, extension, provider, enabled, config) "
                "VALUES(?, ?, ?, ?, ?)",
                (name, extension, provider, int(enabled), json.dumps(config)),
            )
            return int(cur.lastrowid)
        self.execute(
            "UPDATE profiles SET name=?, extension=?, provider=?, enabled=?, config=? "
            "WHERE id=?",
            (name, extension, provider, int(enabled), json.dumps(config), profile_id),
        )
        return profile_id

    def delete_profile(self, profile_id: int) -> None:
        self.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))

    # ------------------------------------------------------------------ calls
    def create_call(self, uuid: str, direction: str, **fields: Any) -> None:
        fields.setdefault("status", "active")
        fields.setdefault("started", time.time())
        if "request" in fields and not isinstance(fields["request"], str):
            fields["request"] = json.dumps(fields["request"])
        cols = ["uuid", "direction", *fields.keys()]
        self.execute(
            f"INSERT INTO calls({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
            [uuid, direction, *fields.values()],
        )

    def update_call(self, uuid: str, **fields: Any) -> None:
        if "transcript" in fields and not isinstance(fields["transcript"], str):
            fields["transcript"] = json.dumps(fields["transcript"])
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE calls SET {sets} WHERE uuid = ?", [*fields.values(), uuid])

    def call(self, uuid: str) -> dict[str, Any] | None:
        r = self.query_one("SELECT * FROM calls WHERE uuid = ?", (uuid,))
        if r:
            r["transcript"] = json.loads(r["transcript"] or "[]")
            r["request"] = json.loads(r["request"]) if r.get("request") else None
        return r

    def calls(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM calls ORDER BY started DESC LIMIT ?", (limit,))
        for r in rows:
            r["transcript"] = json.loads(r["transcript"] or "[]")
            r["request"] = json.loads(r["request"]) if r.get("request") else None
        return rows

    def purge_old_calls(self) -> None:
        days = int(self.get("keep_calls_days") or 0)
        if days > 0:
            self.execute("DELETE FROM calls WHERE started < ?", (time.time() - days * 86400,))

    # -------------------------------------------------------------- schedules
    def schedules(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM schedules ORDER BY name")
        for r in rows:
            r["call"] = json.loads(r["call"])
            r["enabled"] = bool(r["enabled"])
        return rows

    def schedule(self, schedule_id: int) -> dict[str, Any] | None:
        r = self.query_one("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        if r:
            r["call"] = json.loads(r["call"])
            r["enabled"] = bool(r["enabled"])
        return r

    def save_schedule(self, schedule_id: int | None, data: dict[str, Any]) -> int:
        data = dict(data)
        data["call"] = json.dumps(data["call"])
        data["enabled"] = int(bool(data.get("enabled", True)))
        cols = ["name", "enabled", "kind", "at_datetime", "at_time", "weekdays",
                "call", "next_run"]
        values = [data.get(c) for c in cols]
        if schedule_id is None:
            cur = self.execute(
                f"INSERT INTO schedules({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                values,
            )
            return int(cur.lastrowid)
        sets = ", ".join(f"{c} = ?" for c in cols)
        self.execute(f"UPDATE schedules SET {sets} WHERE id = ?", [*values, schedule_id])
        return schedule_id

    def delete_schedule(self, schedule_id: int) -> None:
        self.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
