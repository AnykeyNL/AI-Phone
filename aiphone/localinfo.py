"""Where and when the phone is: the current date and time, the server's time
zone and the location saved on the Settings page. The AI gets this in its
system prompt, and web searches use it to find local weather and news."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_TZ_NAME = re.compile(r"^[A-Za-z]+(?:/[A-Za-z0-9_+-]+)+$|^UTC$")


def timezone_name() -> str:
    """The IANA time zone name, e.g. ``Europe/Amsterdam`` (empty if unknown)."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if _TZ_NAME.match(tz):
        return tz
    try:
        tz = Path("/etc/timezone").read_text().strip()
        if _TZ_NAME.match(tz):
            return tz
    except OSError:
        pass
    try:
        target = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in target:
            tz = target.split("/zoneinfo/", 1)[1]
            if _TZ_NAME.match(tz):
                return tz
    except OSError:
        pass
    return ""


def now_text() -> str:
    """For example 'Sunday 8 November 2026, 14:05 (Europe/Amsterdam)'."""
    now = datetime.now()
    text = f"{now:%A} {now.day} {now:%B %Y, %H:%M}"
    tz = timezone_name() or time.tzname[time.localtime().tm_isdst > 0]
    return f"{text} ({tz})" if tz else text


def location(settings: dict[str, Any]) -> dict[str, str]:
    city = (settings.get("location_city") or "").strip()
    region = (settings.get("location_region") or "").strip()
    country = (settings.get("location_country") or "").strip()
    return {k: v for k, v in (("city", city), ("region", region), ("country", country)) if v}


def location_text(settings: dict[str, Any]) -> str:
    return ", ".join(location(settings).values())


def search_location(settings: dict[str, Any]) -> dict[str, str] | None:
    """``user_location`` for the Anthropic and OpenAI web search tools."""
    loc = location(settings)
    out: dict[str, str] = {"type": "approximate"}
    if loc.get("city"):
        out["city"] = loc["city"]
    if loc.get("region"):
        out["region"] = loc["region"]
    country = loc.get("country", "")
    if re.fullmatch(r"[A-Za-z]{2}", country):  # the tools want an ISO 3166-1 code
        out["country"] = country.upper()
    tz = timezone_name()
    if tz and tz != "UTC":
        out["timezone"] = tz
    return out if len(out) > 1 else None


def prompt_context(settings: dict[str, Any], web_search: bool) -> str:
    """Lines appended to every system prompt."""
    lines = [f"Current date and time: {now_text()}."]
    where = location_text(settings)
    if where:
        lines.append(f"The phone is in {where}. Assume questions about the weather, local "
                     "news or opening hours are about this place unless the caller names "
                     "another one.")
    if web_search:
        lines.append(
            "You can look up live information, such as the weather, news, sports results, "
            "opening hours or prices, with the web_search tool. Use it whenever the answer "
            "depends on current facts; never say you have no access to live information. "
            "Before you search, say a very short phrase such as 'One moment, let me check.' "
            "Then give the answer in one or two spoken sentences, with rounded numbers, "
            "and without web addresses or source names unless the caller asks.")
    return "\n".join(lines)
