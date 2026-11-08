"""Web interface and HTTP API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import __version__, asterisk, localinfo, updater
from ..providers import PROVIDERS, default_profile_config, provider_fields
from ..scheduler import describe, next_run

log = logging.getLogger("aiphone.web")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

SECRET_KEYS = ["openai_api_key", "anthropic_api_key", "deepgram_api_key", "elevenlabs_api_key",
               "update_token"]
PUBLIC_PATHS = ("/login", "/setup", "/static", "/internal/", "/api/webhook/")


# ------------------------------------------------------------------ helpers
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        test = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(test.hex(), digest)
    except ValueError:
        return False


def detect_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # no packet is sent
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "…" + value[-4:] if len(value) > 12 else "••••"


def fmt_time(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(start: float | None, end: float | None) -> str:
    if not start:
        return ""
    secs = int((end or time.time()) - start)
    return f"{secs // 60}:{secs % 60:02d}"


def system_timezone() -> str:
    return localinfo.timezone_name() or time.tzname[0]


templates.env.filters["fmt_time"] = fmt_time
templates.env.globals["fmt_duration"] = fmt_duration
templates.env.globals["describe_schedule"] = describe
templates.env.globals["version"] = __version__


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append({"kind": kind, "text": message})


def form_bool(form, key: str) -> bool:
    return form.get(key) in ("on", "true", "1", "yes")


def parse_field(field: dict[str, Any], form) -> Any:
    raw = form.get(f"cfg_{field['key']}")
    if field["type"] == "checkbox":
        return raw in ("on", "true", "1")
    if raw is None:
        return field.get("default")
    if field["type"] == "number":
        try:
            num = float(raw)
            return int(num) if num.is_integer() and not isinstance(field["default"], float) else num
        except ValueError:
            return field.get("default")
    return str(raw)


# ---------------------------------------------------------------------- app
def create_app(db, manager, scheduler) -> FastAPI:
    app = FastAPI(title="AI-Phone", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        ctx.update(request=request, flashes=request.session.pop("flash", []),
                   path=request.url.path)
        return templates.TemplateResponse(request, name, ctx)

    async def apply_asterisk(request: Request | None = None) -> tuple[bool, str]:
        try:
            asterisk.write_configs(db)
        except OSError as exc:
            msg = f"Could not write Asterisk configuration: {exc}"
            if request:
                flash(request, msg, "error")
            return False, msg
        try:
            ami = await manager.ami()
            output = await ami.reload()
            if request:
                flash(request, "Asterisk configuration updated and reloaded.")
            return True, output
        except Exception as exc:  # noqa: BLE001
            msg = f"Configuration written, but Asterisk could not be reloaded: {exc}"
            if request:
                flash(request, msg, "error")
            return False, msg

    app.state.apply_asterisk = apply_asterisk

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if not path.startswith(PUBLIC_PATHS):
            if not db.get("admin_password_hash"):
                return RedirectResponse("/setup", 303)
            if not request.session.get("user"):
                if path.startswith("/api/"):
                    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
                    if not token or not hmac.compare_digest(token, db.get("webhook_token") or ""):
                        return JSONResponse({"error": "unauthorized"}, 401)
                else:
                    return RedirectResponse(f"/login?next={path}", 303)
        return await call_next(request)

    # SessionMiddleware must wrap the auth middleware, so add it last.
    app.add_middleware(SessionMiddleware, secret_key=db.get("session_secret"),
                       same_site="strict", max_age=14 * 86400, session_cookie="aiphone")

    # ------------------------------------------------------------ auth pages
    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        if db.get("admin_password_hash"):
            return RedirectResponse("/", 303)
        return render(request, "setup.html")

    @app.post("/setup")
    async def setup_submit(request: Request):
        if db.get("admin_password_hash"):
            return RedirectResponse("/", 303)
        form = await request.form()
        pw, pw2 = form.get("password", ""), form.get("password2", "")
        if len(pw) < 8 or pw != pw2:
            flash(request, "Passwords must match and be at least 8 characters.", "error")
            return RedirectResponse("/setup", 303)
        db.set("admin_password_hash", hash_password(pw))
        request.session["user"] = "admin"
        await apply_asterisk()
        flash(request, "Welcome! Add your API keys on the Settings page to get started.")
        return RedirectResponse("/settings", 303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return render(request, "login.html", next=request.query_params.get("next", "/"))

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        nxt = form.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        if check_password(form.get("password", ""), db.get("admin_password_hash") or ""):
            request.session["user"] = "admin"
            return RedirectResponse(nxt, 303)
        await asyncio.sleep(1)
        flash(request, "Wrong password.", "error")
        return RedirectResponse(f"/login?next={nxt}", 303)

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    # ------------------------------------------------------------- dashboard
    async def status_data() -> dict[str, Any]:
        settings = db.all_settings()
        data: dict[str, Any] = {"ami": False, "asterisk_error": "", "lines": [], "trunk": "",
                                "active": [], "version": manager.version}
        contacts = {}
        try:
            ami = await manager.ami()
            contacts = await ami.contacts()
            if asterisk.trunk_active(settings):
                data["trunk"] = await ami.trunk_status() or "Not registered"
            data["ami"] = True
        except Exception as exc:  # noqa: BLE001
            data["asterisk_error"] = str(exc) or type(exc).__name__
        for line in settings["lines"]:
            c = contacts.get(line["username"])
            data["lines"].append({"id": line["id"], "label": line["label"],
                                  "enabled": line["enabled"], "registered": bool(c),
                                  "status": c["status"] if c else "",
                                  "ip": c["ip"] if c else ""})
        for uid, s in manager.sessions.items():
            data["active"].append({"uuid": uid, "direction": s.info.direction,
                                   "profile": s.info.profile["name"], "line": s.info.line,
                                   "duration": fmt_duration(s.info.started, None)})
        missing = []
        for p in db.profiles():
            if p["enabled"]:
                for key in PROVIDERS[p["provider"]]["requires"]:
                    if not settings.get(key):
                        missing.append(f"{p['name']} needs {key.replace('_', ' ')}")
        if any(p["config"].get("tts_provider") == "elevenlabs" for p in db.profiles()
               if p["provider"] == "claude_pipeline") and not settings.get("elevenlabs_api_key"):
            missing.append("ElevenLabs voice selected but no ElevenLabs API key")
        data["warnings"] = missing
        upd = settings.get("update_last_check") or {}
        data["update_available"] = bool(upd.get("available")) and \
            upd.get("latest") != updater.installed().get("commit")
        return data

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return render(request, "dashboard.html", status=await status_data(),
                      calls=db.calls(10), profiles=db.profiles(),
                      lines=db.get("lines"))

    @app.get("/api/status")
    async def api_status():
        return await status_data()

    # -------------------------------------------------------------- profiles
    @app.get("/profiles", response_class=HTMLResponse)
    async def profiles_page(request: Request):
        return render(request, "profiles.html", profiles=db.profiles(), providers=PROVIDERS,
                      default_extension=db.get("default_extension"))

    @app.get("/profiles/{pid}", response_class=HTMLResponse)
    async def profile_edit(request: Request, pid: str):
        if pid == "new":
            provider = request.query_params.get("provider", "openai_realtime")
            profile = {"id": None, "name": "", "extension": "", "provider": provider,
                       "enabled": True, "config": default_profile_config(provider)}
        else:
            profile = db.profile(int(pid))
            if not profile:
                raise HTTPException(404)
            provider = request.query_params.get("provider", profile["provider"])
            if provider != profile["provider"]:
                cfg = default_profile_config(provider)
                for k in ("instructions", "greeting", "allow_end_call", "output_gain_db"):
                    if k in profile["config"]:
                        cfg[k] = profile["config"][k]
                profile = {**profile, "provider": provider, "config": cfg}
        if provider not in PROVIDERS:
            raise HTTPException(400, "Unknown provider")
        config = default_profile_config(provider)
        config.update(profile["config"])
        profile["config"] = config
        return render(request, "profile_edit.html", profile=profile, providers=PROVIDERS,
                      fields=provider_fields(provider))

    @app.post("/profiles/{pid}")
    async def profile_save(request: Request, pid: str):
        form = await request.form()
        provider = form.get("provider")
        if provider not in PROVIDERS:
            raise HTTPException(400, "Unknown provider")
        name = (form.get("name") or "").strip() or "Unnamed"
        ext = "".join(ch for ch in form.get("extension", "") if ch.isdigit() or ch in "*#")
        if not ext:
            flash(request, "The extension must be a number, e.g. 1 or 100.", "error")
            return RedirectResponse(f"/profiles/{pid}?provider={provider}", 303)
        if ext in {d["digits"] for d in db.get("speed_dials") or []}:
            flash(request, f"Number {ext} is already a speed dial.", "error")
            return RedirectResponse(f"/profiles/{pid}?provider={provider}", 303)
        other = db.profile_by_extension(ext)
        if other and str(other["id"]) != pid:
            flash(request, f"Extension {ext} is already used by {other['name']}.", "error")
            return RedirectResponse(f"/profiles/{pid}?provider={provider}", 303)
        if ext == str(db.get("echo_test_extension")):
            flash(request, f"Extension {ext} is the echo test number.", "error")
            return RedirectResponse(f"/profiles/{pid}?provider={provider}", 303)
        config = {f["key"]: parse_field(f, form) for f in provider_fields(provider)}
        new_id = db.save_profile(None if pid == "new" else int(pid), name, ext, provider,
                                 form_bool(form, "enabled"), config)
        if form_bool(form, "make_default"):
            db.set("default_extension", ext)
        flash(request, f"Profile '{name}' saved.")
        await apply_asterisk(request)
        return RedirectResponse(f"/profiles/{new_id}", 303)

    @app.post("/profiles/{pid}/delete")
    async def profile_delete(request: Request, pid: int):
        p = db.profile(pid)
        if p:
            db.delete_profile(pid)
            flash(request, f"Profile '{p['name']}' deleted.")
            await apply_asterisk(request)
        return RedirectResponse("/profiles", 303)

    # ------------------------------------------------------- phone and ATA
    @app.get("/phone", response_class=HTMLResponse)
    async def phone_page(request: Request):
        settings = db.all_settings()
        return render(request, "phone.html", s=settings,
                      server_ip=settings.get("server_ip") or detect_ip(),
                      status=await status_data(), profiles=db.profiles())

    @app.post("/phone")
    async def phone_save(request: Request):
        form = await request.form()
        lines = db.get("lines")
        for line in lines:
            lid = line["id"]
            line["enabled"] = form_bool(form, f"{lid}_enabled")
            line["label"] = (form.get(f"{lid}_label") or line["label"]).strip()
            user = "".join(ch for ch in form.get(f"{lid}_username", "") if ch.isalnum() or ch in "_-.")
            if user:
                line["username"] = user
            pw = (form.get(f"{lid}_password") or "").strip()
            if pw:
                line["password"] = pw
            if form_bool(form, f"{lid}_regen"):
                line["password"] = secrets.token_urlsafe(12)
        if len({l["username"] for l in lines}) != len(lines):
            flash(request, "Each phone port needs a different SIP username.", "error")
            return RedirectResponse("/phone", 303)
        db.set("lines", lines)
        for key in ("sip_port", "rtp_start", "rtp_end", "autodial_delay", "ring_timeout"):
            try:
                db.set(key, int(form.get(key)))
            except (TypeError, ValueError):
                pass
        for key in ("codecs", "default_extension", "echo_test_extension",
                    "outbound_caller_name", "outbound_caller_number", "server_ip"):
            if key in form:
                db.set(key, str(form.get(key)).strip())
        flash(request, "Phone settings saved.")
        await apply_asterisk(request)
        return RedirectResponse("/phone", 303)

    @app.post("/phone/apply")
    async def phone_apply(request: Request):
        await apply_asterisk(request)
        return RedirectResponse(request.headers.get("referer") or "/phone", 303)

    # ------------------------------------------------- SIP account + speed dial
    @app.get("/speeddial", response_class=HTMLResponse)
    async def speeddial_page(request: Request):
        settings = db.all_settings()
        dials = list(settings.get("speed_dials") or [])
        dials += [{"digits": "", "name": "", "number": "", "enabled": True}] * 3
        return render(request, "speeddial.html", t=settings["trunk"], dials=dials,
                      status=await status_data(), profiles=db.profiles(),
                      echo=settings.get("echo_test_extension"))

    @app.post("/speeddial")
    async def speeddial_save(request: Request):
        form = await request.form()
        trunk = dict(db.get("trunk"))
        trunk["enabled"] = form_bool(form, "trunk_enabled")
        for key in ("provider_name", "host", "username", "auth_username", "from_domain",
                    "codecs", "public_ip"):
            trunk[key] = (form.get(f"trunk_{key}") or "").strip()
        for key in ("port", "dial_timeout"):
            try:
                trunk[key] = int(form.get(f"trunk_{key}"))
            except (TypeError, ValueError):
                pass
        pw = (form.get("trunk_password") or "").strip()
        if pw:
            trunk["password"] = pw
        if trunk["enabled"] and not (trunk["host"] and trunk["username"] and trunk["password"]):
            flash(request, "To use the SIP account, fill in server, username and password.",
                  "error")

        taken = {p["extension"]: f"AI profile '{p['name']}'" for p in db.profiles()}
        taken[str(db.get("echo_test_extension"))] = "the echo test"
        dials, seen = [], set()
        for i, digits in enumerate(form.getlist("sd_digits")):
            digits = asterisk._clean_ext(digits)
            number = asterisk.clean_number(form.getlist("sd_number")[i])
            name = (form.getlist("sd_name")[i] or "").strip()
            if not digits and not number:
                continue
            if not digits or len(number) < 3:
                flash(request, f"Speed dial '{name or digits or number}' needs both a short "
                      "number to dial and a phone number; it was not saved.", "error")
                continue
            if digits in taken or digits in seen:
                flash(request, f"Speed dial {digits} is already used by "
                      f"{taken.get(digits, 'another speed dial')}; it was not saved.", "error")
                continue
            seen.add(digits)
            dials.append({"digits": digits, "name": name or number, "number": number,
                          "enabled": f"sd_on_{i}" in form})
        db.set("trunk", trunk)
        db.set("speed_dials", sorted(dials, key=lambda d: d["digits"]))
        flash(request, "SIP account and speed dials saved.")
        await apply_asterisk(request)
        return RedirectResponse("/speeddial", 303)

    # ------------------------------------------------------ outbound calls
    def call_request_from_form(form) -> dict[str, Any]:
        return {
            "line": form.get("line") or "line1",
            "profile_id": int(form["profile_id"]) if form.get("profile_id") else None,
            "mode": form.get("mode") or "message",
            "text": (form.get("text") or "").strip(),
            "allow_reply": form_bool(form, "allow_reply"),
            "retries": int(form.get("retries") or 0),
            "retry_delay": int(form.get("retry_delay") or 300),
        }

    @app.get("/outbound", response_class=HTMLResponse)
    async def outbound_page(request: Request):
        settings = db.all_settings()
        base = f"http://{settings.get('server_ip') or detect_ip()}:{request.url.port or 80}"
        return render(request, "outbound.html", schedules=db.schedules(),
                      profiles=db.profiles(), lines=settings["lines"],
                      token=settings["webhook_token"], base_url=base,
                      timezone=system_timezone(),
                      now=datetime.now().strftime("%Y-%m-%d %H:%M"))

    @app.post("/outbound/call")
    async def outbound_call(request: Request):
        form = await request.form()
        req = call_request_from_form(form)
        req["source"] = "web interface"
        try:
            uid = await manager.originate(req)
            flash(request, "Ringing the phone…")
            return RedirectResponse(f"/calls/{uid}", 303)
        except Exception as exc:  # noqa: BLE001
            flash(request, f"Could not place the call: {exc}", "error")
            return RedirectResponse(request.headers.get("referer") or "/outbound", 303)

    @app.get("/schedules/{sid}", response_class=HTMLResponse)
    async def schedule_edit(request: Request, sid: str):
        settings = db.all_settings()
        if sid == "new":
            sched = {"id": None, "name": "", "enabled": True, "kind": "daily",
                     "at_time": "08:00", "at_datetime": "", "weekdays": "0,1,2,3,4",
                     "call": {"line": "line1", "profile_id": None, "mode": "message",
                              "text": "", "allow_reply": True, "retries": 1,
                              "retry_delay": 300}}
        else:
            sched = db.schedule(int(sid))
            if not sched:
                raise HTTPException(404)
        return render(request, "schedule_edit.html", sched=sched, profiles=db.profiles(),
                      lines=settings["lines"], timezone=system_timezone())

    @app.post("/schedules/{sid}")
    async def schedule_save(request: Request, sid: str):
        form = await request.form()
        data = {
            "name": (form.get("name") or "").strip() or "Unnamed",
            "enabled": form_bool(form, "enabled"),
            "kind": form.get("kind") if form.get("kind") in ("once", "daily", "weekly") else "daily",
            "at_datetime": form.get("at_datetime") or None,
            "at_time": form.get("at_time") or "08:00",
            "weekdays": ",".join(form.getlist("weekdays")),
            "call": call_request_from_form(form),
        }
        data["next_run"] = next_run(data) if data["enabled"] else None
        if data["kind"] == "once" and not data["next_run"] and data["enabled"]:
            flash(request, "That time is in the past; pick a future date and time.", "error")
        new_id = db.save_schedule(None if sid == "new" else int(sid), data)
        if sid != "new":
            db.execute("UPDATE schedules SET last_run = NULL WHERE id = ? AND kind = 'once'",
                       (new_id,))
        flash(request, f"Schedule '{data['name']}' saved.")
        return RedirectResponse("/outbound", 303)

    @app.post("/schedules/{sid}/delete")
    async def schedule_delete(request: Request, sid: int):
        db.delete_schedule(sid)
        flash(request, "Schedule deleted.")
        return RedirectResponse("/outbound", 303)

    @app.post("/schedules/{sid}/run")
    async def schedule_run(request: Request, sid: int):
        s = db.schedule(sid)
        if s:
            req = dict(s["call"])
            req["source"] = f"schedule: {s['name']} (manual)"
            try:
                uid = await manager.originate(req)
                return RedirectResponse(f"/calls/{uid}", 303)
            except Exception as exc:  # noqa: BLE001
                flash(request, f"Could not place the call: {exc}", "error")
        return RedirectResponse("/outbound", 303)

    @app.post("/outbound/token")
    async def regenerate_token(request: Request):
        db.set("webhook_token", secrets.token_urlsafe(24))
        flash(request, "New webhook token created. Update any scripts that use the old one.")
        return RedirectResponse("/outbound", 303)

    async def api_place_call(payload: dict[str, Any], source: str) -> JSONResponse:
        profile_id = payload.get("profile_id")
        if not profile_id and payload.get("extension"):
            p = db.profile_by_extension(str(payload["extension"]))
            if not p:
                return JSONResponse({"error": "unknown extension"}, 400)
            profile_id = p["id"]
        req = {
            "line": payload.get("line") or "line1",
            "profile_id": int(profile_id) if profile_id else None,
            "mode": payload.get("mode") if payload.get("mode") in ("message", "conversation")
            else "message",
            "text": str(payload.get("text") or payload.get("message") or ""),
            "allow_reply": bool(payload.get("allow_reply", False)),
            "retries": int(payload.get("retries") or 0),
            "retry_delay": int(payload.get("retry_delay") or 300),
            "source": source,
        }
        try:
            uid = await manager.originate(req)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, 409)
        return JSONResponse({"ok": True, "call_id": uid})

    @app.post("/api/call")
    async def api_call(request: Request):
        return await api_place_call(await request.json(), "API")

    @app.api_route("/api/webhook/{token}", methods=["GET", "POST"])
    async def webhook(request: Request, token: str):
        if not hmac.compare_digest(token, db.get("webhook_token") or ""):
            return JSONResponse({"error": "unauthorized"}, 401)
        payload: dict[str, Any] = dict(request.query_params)
        if request.method == "POST":
            ctype = request.headers.get("content-type", "")
            if "json" in ctype:
                body = await request.json()
                if isinstance(body, dict):
                    payload.update(body)
            else:
                payload.update(dict(await request.form()))
        for key in ("allow_reply",):
            if isinstance(payload.get(key), str):
                payload[key] = payload[key].lower() in ("1", "true", "yes", "on")
        return await api_place_call(payload, "webhook")

    # -------------------------------------------------------------- call log
    @app.get("/calls", response_class=HTMLResponse)
    async def calls_page(request: Request):
        return render(request, "calls.html", calls=db.calls(200))

    @app.get("/calls/{uid}", response_class=HTMLResponse)
    async def call_detail(request: Request, uid: str):
        c = db.call(uid)
        if not c:
            raise HTTPException(404)
        return render(request, "call_detail.html", c=c,
                      live=c["status"] in ("ringing", "connecting", "active"),
                      can_hangup=uid in manager.sessions)

    @app.get("/api/calls/{uid}")
    async def api_call_detail(uid: str):
        c = db.call(uid)
        if not c:
            raise HTTPException(404)
        return c

    @app.post("/calls/{uid}/hangup")
    async def call_hangup(request: Request, uid: str):
        s = manager.sessions.get(uid)
        if s:
            s.hangup("ended from web interface", after_playback=False)
        return RedirectResponse(f"/calls/{uid}", 303)

    # -------------------------------------------------------------- settings
    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        s = db.all_settings()
        repo, branch = updater.repo_and_branch(db)
        return render(request, "settings.html", s=s,
                      masked={k: mask(s.get(k, "")) for k in SECRET_KEYS},
                      install=updater.installed(), repo=repo, branch=branch,
                      upd=s.get("update_last_check") or {},
                      timezone=system_timezone(), server_ip=detect_ip())

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        for key in SECRET_KEYS:
            value = (form.get(key) or "").strip()
            if form_bool(form, f"clear_{key}"):
                db.set(key, "")
            elif value:
                db.set(key, value)
        for key in ("max_call_seconds", "silence_timeout", "keep_calls_days"):
            try:
                db.set(key, max(0, int(form.get(key))))
            except (TypeError, ValueError):
                pass
        db.set("log_transcripts", form_bool(form, "log_transcripts"))
        for key in ("location_city", "location_region", "location_country"):
            if key in form:
                db.set(key, (form.get(key) or "").strip()[:80])
        flash(request, "Settings saved.")
        return RedirectResponse("/settings", 303)

    @app.post("/settings/updates")
    async def update_settings(request: Request):
        form = await request.form()
        db.set("update_repo", (form.get("update_repo") or "").strip())
        db.set("update_branch", (form.get("update_branch") or "").strip())
        token = (form.get("update_token") or "").strip()
        if form_bool(form, "clear_update_token"):
            db.set("update_token", "")
        elif token:
            db.set("update_token", token)
        db.set("auto_update", form_bool(form, "auto_update"))
        db.set("auto_update_time", form.get("auto_update_time") or "03:30")
        result = await updater.check(db)
        if result.get("error"):
            flash(request, f"Could not check for updates: {result['error']}", "error")
        elif result["available"]:
            flash(request, "An update is available.")
        else:
            flash(request, "AI-Phone is up to date.")
        return RedirectResponse("/settings#updates", 303)

    @app.post("/settings/update")
    async def run_update(request: Request):
        if manager.sessions:
            flash(request, "A call is in progress; update after the call.", "error")
            return RedirectResponse("/settings#updates", 303)
        try:
            updater.start_update()
        except Exception as exc:  # noqa: BLE001
            flash(request, f"Could not start the update: {exc}", "error")
            return RedirectResponse("/settings#updates", 303)
        db.set("update_requested_at", time.time())
        return RedirectResponse("/settings/update", 303)

    @app.get("/settings/update", response_class=HTMLResponse)
    async def update_progress(request: Request):
        status = updater.log_status(db.get("update_requested_at") or 0)
        return render(request, "update.html", status=status)

    @app.get("/api/update")
    async def update_status():
        return updater.log_status(db.get("update_requested_at") or 0)

    def journal(lines: int = 300) -> str:
        try:
            out = subprocess.run(
                ["journalctl", "-u", "aiphone", "-u", "asterisk", "-n", str(lines),
                 "--no-pager", "-o", "short-iso"],
                capture_output=True, text=True, timeout=10).stdout
        except Exception as exc:  # noqa: BLE001
            return f"Could not read the system log: {exc}"
        return out or "(no log lines; is the user in the systemd-journal group?)"

    @app.post("/settings/password")
    async def change_password(request: Request):
        form = await request.form()
        if not check_password(form.get("current", ""), db.get("admin_password_hash")):
            flash(request, "Current password is wrong.", "error")
        elif len(form.get("new", "")) < 8 or form.get("new") != form.get("new2"):
            flash(request, "New passwords must match and be at least 8 characters.", "error")
        else:
            db.set("admin_password_hash", hash_password(form["new"]))
            flash(request, "Password changed.")
        return RedirectResponse("/settings", 303)

    @app.post("/settings/timezone")
    async def set_timezone(request: Request):
        form = await request.form()
        tz = (form.get("timezone") or "").strip()
        try:
            subprocess.run(["sudo", "-n", "/usr/bin/timedatectl", "set-timezone", tz],
                           check=True, capture_output=True, timeout=10)
            os.environ["TZ"] = tz
            time.tzset()
            scheduler.refresh_all()
            flash(request, f"Time zone set to {tz}.")
        except Exception as exc:  # noqa: BLE001
            flash(request, f"Could not set time zone: {exc}", "error")
        return RedirectResponse("/settings", 303)

    @app.post("/settings/restart")
    async def restart(request: Request):
        flash(request, "AI-Phone is restarting; this page reloads in a few seconds.")
        asyncio.get_running_loop().call_later(1, lambda: os._exit(3))
        return RedirectResponse("/settings", 303)

    @app.get("/settings/test/{key}")
    async def test_key(key: str):
        import httpx
        s = db.all_settings()
        tests = {
            "openai_api_key": ("https://api.openai.com/v1/models",
                               {"Authorization": f"Bearer {s['openai_api_key']}"}),
            "anthropic_api_key": ("https://api.anthropic.com/v1/models",
                                  {"x-api-key": s["anthropic_api_key"],
                                   "anthropic-version": "2023-06-01"}),
            "deepgram_api_key": ("https://api.deepgram.com/v1/projects",
                                 {"Authorization": f"Token {s['deepgram_api_key']}"}),
            "elevenlabs_api_key": ("https://api.elevenlabs.io/v1/user",
                                   {"xi-api-key": s["elevenlabs_api_key"]}),
        }
        if key not in tests:
            raise HTTPException(404)
        if not s.get(key):
            return {"ok": False, "message": "No key saved"}
        url, headers = tests[key]
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, headers=headers)
            ok = r.status_code < 400
            return {"ok": ok, "message": "Key works" if ok else f"HTTP {r.status_code}: {r.text[:150]}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)}

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request):
        return render(request, "logs.html", logs=journal())

    @app.get("/api/logs")
    async def logs_api():
        return {"log": journal()}

    # ----------------------------------------------------- Asterisk callback
    @app.get("/internal/call/new", response_class=PlainTextResponse)
    async def internal_new_call(request: Request, exten: str = "", line: str = ""):
        if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(403)
        uid = manager.new_inbound(exten, line)
        return uid or ""

    def local_only(request: Request) -> None:
        if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(403)

    @app.get("/internal/dial/start", response_class=PlainTextResponse)
    async def internal_dial_start(request: Request, digits: str = "", line: str = ""):
        local_only(request)
        return manager.speed_dial_started(digits, line)

    @app.get("/internal/dial/answered", response_class=PlainTextResponse)
    async def internal_dial_answered(request: Request, id: str = ""):
        local_only(request)
        if id:
            manager.speed_dial_answered(id)
        return "ok"

    @app.get("/internal/dial/end", response_class=PlainTextResponse)
    async def internal_dial_end(request: Request, id: str = "", status: str = "",
                                answered: str = "", cause: str = ""):
        local_only(request)
        if id:
            manager.speed_dial_ended(id, status, answered, cause)
        return "ok"

    return app
