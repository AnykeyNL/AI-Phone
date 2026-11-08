"""Call manager: AudioSocket server, inbound call setup and outbound calls."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid as uuidlib
from typing import Any

from . import asterisk
from .audiosocket import read_uuid
from .call import CallInfo, CallSession

log = logging.getLogger("aiphone.manager")

# AMI OriginateResponse reason codes
_ORIGINATE_REASONS = {
    "0": "line not registered or unreachable",
    "1": "hung up",
    "3": "no answer",
    "4": "answered",
    "5": "busy",
    "8": "congestion / not registered",
}


class CallManager:
    def __init__(self, db):
        self.db = db
        self.pending: dict[str, tuple[CallInfo, float]] = {}
        self.sessions: dict[str, CallSession] = {}
        self._ami: asterisk.AMIClient | None = None
        self._server: asyncio.base_events.Server | None = None
        self._outbound_requests: dict[str, dict[str, Any]] = {}
        self.version = 0

    # ---------------------------------------------------------------- basics
    def notify(self) -> None:
        self.version += 1

    async def ami(self) -> asterisk.AMIClient:
        boot = self.db.bootstrap()
        if self._ami is None or not self._ami.connected:
            if self._ami is not None:
                await self._ami.close()
            self._ami = asterisk.AMIClient(
                boot.get("ami_host", "127.0.0.1"), int(boot.get("ami_port", 5038)),
                boot.get("ami_username", "aiphone"), boot.get("ami_secret", ""))
            self._ami.add_listener(self._on_ami_event)
            await self._ami.connect()
        return self._ami

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_audiosocket, asterisk.AUDIOSOCKET_HOST, asterisk.AUDIOSOCKET_PORT)
        log.info("AudioSocket server listening on %s:%s",
                 asterisk.AUDIOSOCKET_HOST, asterisk.AUDIOSOCKET_PORT)
        asyncio.create_task(self._housekeeping())

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for s in list(self.sessions.values()):
            s.hangup("service stopping", after_playback=False)
        if self._ami:
            await self._ami.close()

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for uid, (info, created) in list(self.pending.items()):
                if now - created > 180:
                    self.pending.pop(uid, None)
                    if self.db.call(uid) and self.db.call(uid)["status"] in ("connecting", "ringing"):
                        self.db.update_call(uid, status="failed", reason="never connected",
                                            ended=now)
            try:
                self.db.purge_old_calls()
            except Exception:  # noqa: BLE001
                log.exception("Purging old calls failed")

    # --------------------------------------------------------------- inbound
    # ------------------------------------------------- calls via the SIP account
    # Speed-dial calls and incoming calls on the SIP account are bridged by
    # Asterisk between the phone and the provider, without AI-Phone in the
    # audio path. The dialplan reports the start, the answer and the end, so
    # they show up in the call log.
    INCOMING_RESULTS = {
        "BUSY": ("no-answer", "the phone was busy"),
        "NOANSWER": ("no-answer", "nobody answered"),
        "CANCEL": ("no-answer", "the caller hung up before you answered"),
        "CONGESTION": ("failed", "the phone could not be rung"),
        "CHANUNAVAIL": ("failed", "the phone (ATA) is not connected"),
        "": ("failed", "the phone could not be rung"),
    }
    DIAL_RESULTS = {
        "BUSY": ("failed", "busy"),
        "NOANSWER": ("no-answer", "no answer"),
        "CANCEL": ("no-answer", "you hung up before it was answered"),
        "CONGESTION": ("failed", "the SIP provider refused the call"),
        "CHANUNAVAIL": ("failed", "the SIP account is not available"),
        "INVALIDARGS": ("failed", "invalid number"),
    }

    def _line_id(self, line: str) -> str:
        for l in self.db.get("lines") or []:
            if l["username"] == line:
                return l["id"]
        return line

    def speed_dial_started(self, digits: str, line: str) -> str:
        sd = next((x for x in self.db.get("speed_dials") or []
                   if str(x.get("digits")) == digits), {})
        number = asterisk.clean_number(sd.get("number", ""))
        name = (sd.get("name") or "").strip()
        label = f"{name} ({number})" if name else number or digits
        uid = str(uuidlib.uuid4())
        self.db.create_call(uid, "speed-dial", line=self._line_id(line),
                            profile=f"{digits} → {label}", provider="sip",
                            status="ringing")
        self.notify()
        log.info("Speed dial %s: calling %s", digits, label)
        return uid

    def incoming_sip_started(self, caller: str, caller_name: str = "") -> str:
        number = asterisk.clean_number(caller or "") or "unknown number"
        name = re.sub(r"[^\w .,'()-]", "", caller_name or "").strip()
        label = f"{name} ({number})" if name and name != number else number
        uid = str(uuidlib.uuid4())
        self.db.create_call(uid, "incoming-sip", line="", profile=label, provider="sip",
                            status="ringing")
        self.notify()
        log.info("Incoming call on the SIP account from %s", label)
        return uid

    def speed_dial_answered(self, uid: str) -> None:
        c = self.db.call(uid)
        if c and c["status"] == "ringing":
            self.db.update_call(uid, status="active", answered=time.time())
            self.notify()

    def speed_dial_ended(self, uid: str, dial_status: str, answered_secs: str = "",
                         cause: str = "", peer: str = "") -> None:
        c = self.db.call(uid)
        if not c or c["status"] not in ("ringing", "active"):
            return
        now = time.time()
        fields: dict[str, Any] = {"ended": now}
        incoming = c["direction"] == "incoming-sip"
        peer = re.sub(r"^PJSIP/|-[0-9a-f]+$", "", peer or "")
        if incoming and peer:  # which phone port answered
            fields["line"] = self._line_id(peer)
        dial_status = (dial_status or "").upper()
        if dial_status == "ANSWER" or c["status"] == "active":
            try:
                talk = float(answered_secs)
            except ValueError:
                talk = 0
            fields.update(status="completed", reason="call ended")
            if talk > 0:
                fields["answered"] = now - talk
            elif not c.get("answered"):
                fields["answered"] = now
        else:
            results = self.INCOMING_RESULTS if incoming else self.DIAL_RESULTS
            status, reason = results.get(
                dial_status, ("failed", f"call failed ({dial_status or 'unknown'})"))
            if cause and dial_status not in results:
                reason += f", cause {cause}"
            fields.update(status=status, reason=reason)
        self.db.update_call(uid, **fields)
        self.notify()
        log.info("Speed-dial call %s ended: %s", uid, fields.get("reason"))

    def new_inbound(self, exten: str, line: str) -> str | None:
        """Called by the Asterisk dialplan (via CURL) when a phone dials ``exten``."""
        profile = self.db.profile_by_extension(exten)
        if not profile or not profile["enabled"]:
            log.warning("No enabled profile for extension %s", exten)
            return None
        for l in self.db.get("lines") or []:
            if l["username"] == line:
                line = l["id"]
        uid = str(uuidlib.uuid4())
        info = CallInfo(uuid=uid, direction="inbound", profile=profile, line=line)
        self.pending[uid] = (info, time.time())
        self.db.create_call(uid, "inbound", line=line, profile_id=profile["id"],
                            profile=profile["name"], provider=profile["provider"],
                            status="connecting")
        self.notify()
        return uid

    async def _handle_audiosocket(self, reader: asyncio.StreamReader,
                                  writer: asyncio.StreamWriter) -> None:
        try:
            uid = await read_uuid(reader)
        except Exception as exc:  # noqa: BLE001
            log.warning("Bad AudioSocket connection: %s", exc)
            writer.close()
            return
        entry = self.pending.pop(uid, None)
        if entry is None:
            log.warning("Unknown call id %s", uid)
            writer.close()
            return
        info = entry[0]
        # Reload the profile so edits made while ringing take effect.
        info.profile = self.db.profile(info.profile["id"]) or info.profile
        session = CallSession(self, info, reader, writer)
        self.sessions[uid] = session
        try:
            await session.run()
        finally:
            self.sessions.pop(uid, None)
            req = self._outbound_requests.pop(uid, None)
            if req is not None:
                self.notify()

    # -------------------------------------------------------------- outbound
    async def originate(self, request: dict[str, Any]) -> str:
        """Ring a phone. ``request`` keys: line, profile_id, mode ('message' or
        'conversation'), text, allow_reply, retries, retry_delay, source."""
        settings = self.db.all_settings()
        lines = {l["id"]: l for l in settings["lines"]}
        line = lines.get(request.get("line") or "line1")
        if not line or not line.get("enabled"):
            raise ValueError(f"Line {request.get('line')!r} is not enabled")
        profile_id = request.get("profile_id")
        profile = self.db.profile(int(profile_id)) if profile_id else None
        if profile is None:
            profile = self.db.profile_by_extension(settings.get("default_extension") or "")
        if profile is None:
            profiles = [p for p in self.db.profiles() if p["enabled"]]
            if not profiles:
                raise ValueError("No enabled AI profile")
            profile = profiles[0]
        uid = str(uuidlib.uuid4())
        info = CallInfo(uuid=uid, direction="outbound", profile=profile,
                        line=line["id"], request=request)
        self.pending[uid] = (info, time.time())
        self._outbound_requests[uid] = request
        self.db.create_call(uid, "outbound", line=line["id"], profile_id=profile["id"],
                            profile=profile["name"], provider=profile["provider"],
                            status="ringing", request=request)
        self.notify()
        name = (settings.get("outbound_caller_name") or "AI Phone").replace('"', "")
        number = settings.get("outbound_caller_number") or "999"
        ring_timeout = int(settings.get("ring_timeout") or 40)
        try:
            ami = await self.ami()
            resp = await ami.action(
                "Originate", _action_id=f"orig-{uid}",
                Channel=f"PJSIP/{asterisk._clean(line['username'])}",
                Context="aiphone-outbound", Exten="s", Priority="1",
                Timeout=str(ring_timeout * 1000), CallerID=f'"{name}" <{number}>',
                Variable=f"AIPHONE_UUID={uid}", Async="true")
        except Exception as exc:  # noqa: BLE001
            self.pending.pop(uid, None)
            self.db.update_call(uid, status="failed", reason=f"error: {exc}", ended=time.time())
            raise
        if resp.get("Response") != "Success":
            self.pending.pop(uid, None)
            self.db.update_call(uid, status="failed", reason=resp.get("Message", "originate failed"),
                                ended=time.time())
            raise RuntimeError(resp.get("Message", "Originate failed"))
        log.info("Ringing %s for outbound call %s", line["id"], uid)
        return uid

    def _on_ami_event(self, msg: dict[str, str]) -> None:
        if msg.get("Event") != "OriginateResponse":
            return
        action_id = msg.get("ActionID", "")
        if not action_id.startswith("orig-"):
            return
        uid = action_id[5:]
        if msg.get("Response") == "Success":
            return
        reason = _ORIGINATE_REASONS.get(msg.get("Reason", ""), f"reason {msg.get('Reason')}")
        log.info("Outbound call %s not answered: %s", uid, reason)
        self.pending.pop(uid, None)
        self.db.update_call(uid, status="no-answer", reason=reason, ended=time.time())
        request = self._outbound_requests.pop(uid, None)
        self.notify()
        if request and int(request.get("retries") or 0) > 0:
            retry = dict(request)
            retry["retries"] = int(request["retries"]) - 1
            delay = max(30, int(request.get("retry_delay") or 300))
            log.info("Retrying in %s s (%s retries left)", delay, retry["retries"])
            asyncio.get_running_loop().call_later(
                delay, lambda: asyncio.create_task(self._safe_originate(retry)))

    async def _safe_originate(self, request: dict[str, Any]) -> None:
        try:
            await self.originate(request)
        except Exception:  # noqa: BLE001
            log.exception("Retry failed")
