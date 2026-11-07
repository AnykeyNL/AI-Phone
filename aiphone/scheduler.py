"""Scheduled outbound calls (once, daily or on chosen weekdays).

Times are in the server's local time zone, which the installer sets and the
Settings page shows.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger("aiphone.scheduler")


def _parse_hhmm(value: str) -> tuple[int, int]:
    h, m = (value or "08:00").split(":")[:2]
    return int(h), int(m)


def next_run(schedule: dict[str, Any], after: float | None = None) -> float | None:
    """Next run time (epoch seconds) strictly after ``after``, or None."""
    after = time.time() if after is None else after
    now = datetime.fromtimestamp(after).astimezone()
    kind = schedule.get("kind")
    if kind == "once":
        if not schedule.get("at_datetime"):
            return None
        try:
            when = datetime.fromisoformat(schedule["at_datetime"]).astimezone()
        except ValueError:
            return None
        ts = when.timestamp()
        return ts if ts > after and not schedule.get("last_run") else None
    hour, minute = _parse_hhmm(schedule.get("at_time") or "08:00")
    days = {int(d) for d in str(schedule.get("weekdays") or "").split(",") if d.strip()}
    if kind == "daily" or not days:
        days = set(range(7))
    for offset in range(0, 8):
        day = (now + timedelta(days=offset)).replace(hour=hour, minute=minute,
                                                     second=0, microsecond=0)
        if day.weekday() in days and day.timestamp() > after:
            return day.timestamp()
    return None


def describe(schedule: dict[str, Any]) -> str:
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    kind = schedule.get("kind")
    if kind == "once":
        return f"Once at {(schedule.get('at_datetime') or '').replace('T', ' ')}"
    if kind == "daily":
        return f"Every day at {schedule.get('at_time')}"
    days = [names[int(d)] for d in str(schedule.get("weekdays") or "").split(",") if d.strip()]
    return f"{', '.join(days) or 'Every day'} at {schedule.get('at_time')}"


class Scheduler:
    def __init__(self, db, manager):
        self.db = db
        self.manager = manager
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self.refresh_all()
        self._task = asyncio.create_task(self._loop())

    def refresh_all(self) -> None:
        for s in self.db.schedules():
            self.refresh(s)

    def refresh(self, schedule: dict[str, Any]) -> None:
        nxt = next_run(schedule) if schedule["enabled"] else None
        self.db.execute("UPDATE schedules SET next_run = ? WHERE id = ?", (nxt, schedule["id"]))

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            now = time.time()
            for s in self.db.schedules():
                if not s["enabled"] or not s["next_run"] or s["next_run"] > now:
                    continue
                late = now - s["next_run"]
                self.db.execute("UPDATE schedules SET last_run = ? WHERE id = ?", (now, s["id"]))
                s["last_run"] = now
                nxt = next_run(s, after=now)
                self.db.execute("UPDATE schedules SET next_run = ? WHERE id = ?", (nxt, s["id"]))
                if late > 600:
                    log.warning("Skipping schedule %s: %d s late (server was down?)",
                                s["name"], late)
                    continue
                request = dict(s["call"])
                request["source"] = f"schedule: {s['name']}"
                log.info("Running schedule %s", s["name"])
                try:
                    await self.manager.originate(request)
                except Exception:  # noqa: BLE001
                    log.exception("Scheduled call %s failed", s["name"])
