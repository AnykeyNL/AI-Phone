"""Self-update: compare the installed version with the GitHub repository and
start the root update service (``aiphone-update.service``, see update.sh)."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("aiphone.updater")

APP_DIR = Path(__file__).resolve().parent.parent
INSTALL_INFO = APP_DIR / ".install.json"
UPDATE_LOG = Path("/var/log/aiphone-update.log")
DEFAULT_REPO = "AnykeyNL/AI-Phone"


def installed() -> dict[str, Any]:
    try:
        return json.loads(INSTALL_INFO.read_text())
    except (OSError, ValueError):
        return {}


def repo_and_branch(db) -> tuple[str, str]:
    info = installed()
    repo = (db.get("update_repo") or "").strip() or info.get("repo") or DEFAULT_REPO
    branch = (db.get("update_branch") or "").strip() or info.get("branch") or "main"
    return repo, branch


async def check(db) -> dict[str, Any]:
    """Ask GitHub whether there are commits newer than the installed one."""
    repo, branch = repo_and_branch(db)
    current = installed().get("commit", "")
    token = (db.get("update_token") or "").strip()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "AI-Phone-updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    result: dict[str, Any] = {"checked_at": time.time(), "repo": repo, "branch": branch,
                              "installed": current, "available": False, "commits": []}
    base = f"https://api.github.com/repos/{repo}"
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            r = await client.get(f"{base}/commits/{branch}")
            if r.status_code == 404:
                raise RuntimeError("Repository or branch not found. For a private repository, "
                                   "save a GitHub token with read access.")
            if r.status_code == 401:
                raise RuntimeError("GitHub rejected the token.")
            r.raise_for_status()
            latest = r.json()
            result["latest"] = latest["sha"]
            result["latest_message"] = latest["commit"]["message"].splitlines()[0]
            result["latest_date"] = latest["commit"]["committer"]["date"]
            if current and current != latest["sha"]:
                c = await client.get(f"{base}/compare/{current}...{latest['sha']}")
                if c.status_code == 200:
                    data = c.json()
                    result["available"] = data.get("ahead_by", 0) > 0
                    result["commits"] = [
                        {"sha": x["sha"][:7], "message": x["commit"]["message"].splitlines()[0],
                         "date": x["commit"]["committer"]["date"]}
                        for x in reversed(data.get("commits", []))]
                else:  # installed commit unknown to GitHub (e.g. local changes)
                    result["available"] = True
            elif not current:
                result["available"] = True
                result["note"] = "The installed version is unknown, so updating is recommended."
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc) or type(exc).__name__
    db.set("update_last_check", result)
    return result


def start_update() -> None:
    """Start the update in a separate root service, so it survives our restart."""
    with tempfile.TemporaryFile() as err:
        proc = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "start", "--no-block",
                               "aiphone-update.service"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=err, timeout=30)
        if proc.returncode:
            err.seek(0)
            raise RuntimeError(err.read().decode(errors="replace").strip()
                               or f"exit code {proc.returncode}")


def read_log() -> str:
    try:
        return UPDATE_LOG.read_text(errors="replace")[-20000:]
    except OSError:
        return ""


async def auto_update_loop(db, manager) -> None:
    """Once a day at the chosen time: check, and update when nobody is on the phone."""
    last_day = ""
    while True:
        await asyncio.sleep(60)
        if not db.get("auto_update"):
            continue
        now = time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        h, m = (db.get("auto_update_time") or "03:30").split(":")[:2]
        minutes_after = (now.tm_hour * 60 + now.tm_min) - (int(h) * 60 + int(m))
        if today == last_day or not 0 <= minutes_after < 180:
            continue
        if manager.sessions:
            continue  # try again next minute, after the call
        last_day = today
        result = await check(db)
        if result.get("available") and not result.get("error"):
            log.info("Automatic update: %d new change(s)", len(result["commits"]))
            try:
                start_update()
            except Exception:  # noqa: BLE001
                log.exception("Could not start the automatic update")
