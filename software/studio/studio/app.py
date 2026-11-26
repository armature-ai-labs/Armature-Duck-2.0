"""Studio web server: static UI, auth, logs, diagnostics bundle, and WebSocket hub.

WebSocket /ws checks Host and Origin on handshake; unauthenticated clients are accepted and closed with 4401
so the UI can redirect to login cleanly instead of seeing 1006.
"""
import asyncio
import contextlib
import gzip
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .auth import COOKIE, REMEMBER_S, Auth, OriginGuard
from . import helper
from .rpc import open_conn
from .servo import boot_id, view_from_robotd
from .sshkeys import BadKey, SshKeys
try:
    from . import phone_bridge
except Exception:
    phone_bridge = None
from .state import summarize
from .pad import Refused as PadRefused, robot_view
from .ai_connections import AiError
from .ai_service import AiService
from .audio_interaction import MICROPHONE_SOURCES, microphone_request
from .microphone_settings import MicrophoneSettings
from .maintenance import make_handler
from .updates import Updates, UpdateError, handle as handle_updates, safe_during_update

VERSION = "0.1.0-dev"
RUNTIME_ROOT = Path(__file__).resolve().parents[2]
try:
    SUITE_VERSION = json.loads((RUNTIME_ROOT / 'suite-version.json').read_bytes())['version']
except (OSError,ValueError,KeyError):
    SUITE_VERSION = None
MODEL_UPLOAD_LIMIT = 64 * 1024 * 1024
MODEL_OP_LIMIT = 32768
STATE_HZ = 10
COMMANDS = {"relax", "servo.open", "servo.close", "servo.op", "servo.ports", "imu.op", "calib.op", "pad.op", "onboard.op", "battery.op", "tune.op", "net.op"}
HB_QUIET_S = 5.0
HB_DEAD_S = 30.0
MUSIC_PONG_TIMEOUT_S = 5.0
SERVO_IDLE_S = 60.0
ONBOARD_TICK_S = 0.5
SERVO_MARKER = "servo-session.json"
ROBOTD_PROBE_S = 5.0
PAD_HEAVY = ("skills", "actions", "combos", "sounds")


def overview_diagnostics_active(client, now=None):
    quiet = (time.monotonic() if now is None else now)-client.get("hb_at", 0.)
    return (client.get("visible") is True and client.get("diagnostics_visible") is True
            and client.get("route") in ("/", "/pad") and 0. <= quiet <= HB_QUIET_S)


class HostGuard:
    """ASGI middleware: reject unknown Host headers with 421 to prevent DNS rebinding."""

    def __init__(self, app, guard, log):
        self.app, self.guard, self.log = app, guard, log

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and (scope.get('path') in ('/api/ai', '/api/microphone')
                                       or scope.get('path','').startswith('/api/updates')):
            original_send = send
            async def private_send(message):
                if message['type'] == 'http.response.start':
                    message = {**message, 'headers':[(k,v) for k,v in message.get('headers',[])
                        if k.lower() != b'cache-control'] + [(b'cache-control',b'no-store')]}
                await original_send(message)
            send = private_send
        if scope["type"] in ("http", "websocket"):
            host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1")
            local_ip = (scope.get("server") or (None,))[0]
            if not self.guard.host_ok(host, local_ip):
                self.log.warn("Rejected request with invalid Host header", cat="Connection", raw=repr(host[:100]),
                              hint="If using a custom hostname, add it via --name")
                if scope["type"] == "http":
                    await PlainTextResponse("Invalid Host", status_code=421)(scope, receive, send)
                else:
                    await send({"type": "websocket.close", "code": 4403})
                return
        await self.app(scope, receive, send)


class WebFileResponse(FileResponse):
    """Normalize mtimes in release builds; verify pages/models by served content."""

    def set_stat_headers(self, stat_result):
        self.headers.setdefault("content-length", str(stat_result.st_size))

    def _should_use_range(self, http_if_range):
        return http_if_range == self.headers["etag"]

    def content_etag(self):
        digest = hashlib.sha256()
        with open(self.path, "rb") as source:
            for block in iter(lambda: source.read(64 * 1024), b""):
                digest.update(block)
        return f'"{digest.hexdigest()}"'

    async def __call__(self, scope, receive, send):
        etag = await asyncio.to_thread(self.content_etag)
        self.headers["etag"] = etag
        self.headers["cache-control"] = "no-cache"
        for value in Headers(scope=scope).getlist("if-none-match"):
            if value.strip() == "*" or any(tag.strip() in (etag, "W/" + etag) for tag in value.split(",")):
                return await NotModifiedResponse(self.headers)(scope, receive, send)
        await super().__call__(scope, receive, send)


class AppStatic(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        return WebFileResponse(full_path, status_code=status_code, stat_result=stat_result)


def _client_desc(conn):
    ip = conn.client.host if conn.client else "?"
    ua = conn.headers.get("user-agent", "")
    kind = "Mobile" if any(k in ua for k in ("iPhone", "Android", "Mobile")) else "Desktop"
    return f"{kind} {ip}"


def _bus_take(by, servo=None):
    """Stop robotd and take over the servo bus via root helper if available."""
    if not helper.available():
        if servo is not None:
            servo.helper_available = False
        return True, None
    if servo is not None:
        servo.helper_available = True
    ok, result, detail = helper.bus_take(by)
    if ok:
        return True, None
    return False, helper.friendly(result, detail, "bus-take")


def _bus_give_back(by, servo=None):
    """Restart robotd after handing back the servo bus via root helper if available."""
    if not helper.available():
        if servo is not None:
            servo.helper_available = False
        return True, None
    if servo is not None:
        servo.helper_available = True
    ok, result, detail = helper.bus_give_back(by, soft=True, timeout=60)
    if servo is not None:
        # robotd is still replying in startup phases (old commissioning fork)
        servo.giveback_stuck = bool(isinstance(detail, dict) and detail.get("still_starting"))
    if ok:
        return True, None
    return False, helper.friendly(result, detail, "bus-give-back")


def build_app(*, auth, robotd, log, web_dir, duck, names=(), state_dir=None, dev_autologin=False, servo=None, imu=None, pad=None,
              onboard=None, battery=None, tune=None, net=None, commit=None, ssh_home=None, models=None, sensors=None, updates=None,
              sensor_auto=None, ca_cert=None):
    guard = OriginGuard(names=names)
    ai = AiService(state_dir)
    microphone_settings = MicrophoneSettings(state_dir or getattr(auth, 'dir', None))
    updates = updates or Updates()
    if pad is not None:
        pad.update_guard=updates.installing
    if sensor_auto is not None:
        sensor_auto.busy = updates.installing
    update_commands = set()
    mapping_requests = set()

    def update_readiness(*,software=False,native_transaction=None):
        busy = any(not task.done() for task in update_commands) or model_upload_gate.locked()
        motion = getattr(pad,'motion_studio',None)
        native=updates.native_state()
        own_prepared=(native.get('bundle_schema')=='microduck-controller-payload/1' and native.get('active') is True
            and native.get('phase')=='prepared' and isinstance(native_transaction,str)
            and native.get('transaction_id')==native_transaction)
        # The software updater writes its own marker before this query, so the plain
        # query must not count it or no install can ever pass preflight. Native updates
        # check that marker themselves as root; native recovery still counts it here.
        guarded=((native_transaction is not None and updates.marker.exists())
                 or (native.get('active') is True and not own_prepared))
        if guarded or busy:
            return {'ready':False,'error':'Another update or control command is running; retry shortly'}
        if software:
            # updater/control.py reads the live policy flag after writers have drained.
            return {'ready':True}
        if ((servo is not None and servo.state != 'closed')
            or mapping_requests or (pad is not None and getattr(getattr(pad,'mapping',None),'blocked',False))
            or (motion is not None and (motion.state != 'idle' or motion.session is not None))
            or (imu is not None and (getattr(imu,'_redo_busy',False)
                or getattr(imu,'blocks_updates',lambda:False)()))):
            return {'ready':False,'error':'Close servo, calibration, recording, playback, and motion sessions before retrying'}
        return {'ready':True}
    if imu is not None and commit is not None:
        imu.commit = commit
    if onboard is not None:
        if pad is not None:
            pad.on_estop_passed = onboard.note_estop
        if imu is not None:
            imu.on_check = onboard.note_imu_check
        if commit is not None:
            onboard.commit_service = commit
            commit.onboard = onboard
    web_dir = Path(web_dir)
    clients = {}           # websocket -> {"visible": bool, "by": str, "hb_at": monotonic}
    login_gate = asyncio.Semaphore(1)
    servo_gate = asyncio.Lock()
    model_upload_gate = asyncio.Lock()
    def mapping_readiness():
        # Called in a pad worker: cached flags only. Robot poll latency must
        # not hide a servo takeover or an active IMU calibration session.
        from .pad_mapping import MappingError
        if (servo_gate.locked() or model_upload_gate.locked()
                or (models is not None and getattr(models,'_lock',None) is not None and models._lock.locked())
                or (commit is not None and getattr(commit,'_writing',None) is not None and commit._writing.locked())
                or (servo is not None and servo.state!='closed')
                or (imu is not None and (getattr(imu,'_redo_busy',False)
                    or getattr(imu,'blocks_updates',lambda:False)()))):
            raise MappingError('Finish servo, calibration, and model update sessions before remapping gamepad')
    if pad is not None:pad.mapping_guard=mapping_readiness
    marker = Path(state_dir) / SERVO_MARKER if state_dir and servo is not None else None

    dev_sess = {"csrf": "dev", "auth_at": time.time(), "remember": False}
    open_sess = {"csrf": "open", "auth_at": time.time(), "remember": False}
    if dev_autologin:
        log.warn("Dev mode: localhost requests bypass login", cat="System", hint="For local development only; do not enable on robot")

    def session_of(conn, touch=True):
        token = conn.cookies.get(COOKIE)
        if dev_autologin and conn.client and conn.client.host == "127.0.0.1":
            return token or "dev", dev_sess
        if not auth.has_password():
            return token or "open", open_sess
        return token, auth.session(token, touch=touch)

    def origin_ok(conn):
        return guard.origin_ok(conn.headers.get("origin"), conn.headers.get("host", ""))

    # ---- HTTP ----

    async def read_microphone(message):
        return await asyncio.to_thread(microphone_request, message)

    async def api_microphone(request):
        def error(reason, status):
            return JSONResponse({'ok': False, 'error': reason}, status_code=status)

        _, sess = session_of(request)
        if not sess:
            return error('Please log in first.', 401)
        write = request.method == 'POST'
        if (request.headers.get('x-csrf') != sess['csrf'] or
                ((write or 'origin' in request.headers) and not origin_ok(request))):
            return error('Request verification failed; refresh the page and retry.', 403)
        if write:
            try:
                body = json.loads(await limited_body(request, 1024))
            except OverflowError:
                return error('Microphone request too large.', 413)
            except (ValueError, RecursionError):
                return error('Invalid microphone request format.', 400)
            if not isinstance(body, dict) or set(body) != {'source'} or body['source'] not in MICROPHONE_SOURCES:
                return error('Select a valid microphone input source.', 400)
            if updates.installing():
                return error('Software update in progress; wait for completion.', 409)
        try:
            if write:
                task = asyncio.current_task()
                update_commands.add(task)
                try:
                    data = await microphone_settings.apply(body['source'], read_microphone)
                finally:
                    update_commands.discard(task)
            else:
                data = await read_microphone({'method': 'status'})
            return JSONResponse({'ok': True, 'data': data})
        except (RuntimeError, ValueError, OSError) as exc:
            return error(str(exc), 503)

    async def api_updates(request):
        try:
            _,sess=session_of(request)
            result=await handle_updates(request,updates,sess,auth.has_password(),
                dev_autologin and request.client and request.client.host=='127.0.0.1',origin_ok(request))
            return JSONResponse(result,headers={'Cache-Control':'no-store'})
        except UpdateError as error:
            return JSONResponse({'ok':False,'code':error.code,'error':str(error)},status_code=error.status,headers={'Cache-Control':'no-store'})
        except Exception:
            return JSONResponse({'ok':False,'code':'service','error':'Update service did not complete request; retry shortly'},status_code=503,headers={'Cache-Control':'no-store'})

    async def api_ai(request):
        def response(data, status=200):
            return JSONResponse(data, status_code=status, headers={'Cache-Control':'no-store'})
        _, sess = session_of(request)
        local_dev = dev_autologin and request.client and request.client.host == '127.0.0.1'
        # The general Studio open session is unsuitable for billable external requests.
        if not auth.has_password() and not local_dev:
            return response({'ok':False,'code':'password_required','error':'Set a web password in Settings before using AI connections'},403)
        if not sess:
            return response({'ok':False,'code':'unauthorized','error':'Please log in first'},401)
        if request.method == 'POST' and (not origin_ok(request) or request.headers.get('x-csrf') != sess['csrf']):
            return response({'ok':False,'code':'csrf','error':'Invalid request origin or CSRF token'},403)
        try:
            if request.method == 'GET':
                result = await ai.view()
            else:
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise AiError('oversize','AI request too large',413)
                try:
                    body = json.loads(raw)
                except (ValueError,RecursionError):
                    raise AiError('validation','Request is not valid JSON') from None
                result = await ai.handle(body)
            return response({'ok':True,**result})
        except AiError as error:
            return response({'ok':False,'code':error.code,'error':str(error)},error.status)
        except Exception:
            # Do not log exception details: network libraries may include request credentials.
            return response({'ok':False,'code':'internal','error':'AI request failed; retry shortly'},500)

    async def healthz(request):
        return JSONResponse({"ok": True, "version": VERSION,"suite_version":SUITE_VERSION,"runtime_root":str(RUNTIME_ROOT)})

    async def api_session(request):
        token, sess = session_of(request)
        if not sess:
            return JSONResponse({"ok": False, "has_password": auth.has_password()}, status_code=401)
        return JSONResponse({"ok": True, "csrf": sess["csrf"], "duck": duck, "version": VERSION})

    async def api_login(request):
        if not origin_ok(request):
            return JSONResponse({"ok": False, "error": "Invalid request origin"}, status_code=403)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        ip = request.client.host if request.client else "?"
        if not isinstance(body, dict):
            body = {}
        pw = str(body.get("password", ""))
        # Request body is never logged; do not call log.hide(pw)
        async with login_gate:
            token, why, wait = await asyncio.to_thread(auth.login, pw, ip, bool(body.get("remember")))
        if not token:
            return JSONResponse({"ok": False, "error": why, "wait": wait}, status_code=401)
        resp = JSONResponse({"ok": True})
        # SameSite=Lax allows links from external apps; cross-site writes are blocked by Origin + CSRF
        resp.set_cookie(COOKIE, token, max_age=REMEMBER_S if body.get("remember") else None,
                        httponly=True, samesite="lax", path="/")
        return resp

    async def api_logout(request):
        token, sess = session_of(request)
        if sess and origin_ok(request) and request.headers.get("x-csrf") == sess["csrf"]:
            auth.logout(token)
            log.info(f"Logged out ({_client_desc(request)})", cat="Connection")
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    async def api_password(request):
        """Set, change, or clear the web admin password (writes password.json)."""
        token, sess = session_of(request)
        if not origin_ok(request):
            return JSONResponse({"ok": False, "error": "Invalid request origin"}, status_code=403)
        if not sess:
            return JSONResponse({"ok": False, "error": "Log in first to change password"}, status_code=401)
        if request.headers.get("x-csrf") != sess["csrf"]:
            return JSONResponse({"ok": False, "error": "Request verification failed; refresh and retry"}, status_code=403)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        action = body.get("action", "set")
        ip = request.client.host if request.client else "?"
        if action == "clear":
            if auth.has_password():
                await asyncio.to_thread(auth.clear_password)
                log.info(f"Admin password cleared ({_client_desc(request)})", cat="Connection")
            resp = JSONResponse({"ok": True, "has_password": False})
            resp.delete_cookie(COOKIE, path="/")
            return resp
        if action != "set":
            return JSONResponse({"ok": False, "error": "Unknown action"}, status_code=400)
        pw = body.get("password")
        confirm = body.get("confirm")
        if not isinstance(pw, str) or len(pw) < 6:
            return JSONResponse({"ok": False, "error": "Password must be at least 6 characters"}, status_code=400)
        if pw != confirm:
            return JSONResponse({"ok": False, "error": "Passwords do not match"}, status_code=400)
        had_password = auth.has_password()
        keep = token if had_password and token and token != "open" else None
        try:
            await asyncio.to_thread(auth.set_password, pw, keep)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        resp = JSONResponse({"ok": True, "has_password": True})
        if not had_password:
            new_token, _, _ = await asyncio.to_thread(auth.login, pw, ip, False)
            if new_token:
                resp.set_cookie(COOKIE, new_token, httponly=True, samesite="lax", path="/")
        log.info(f"Admin password {'set' if not had_password else 'updated'} ({_client_desc(request)})", cat="Connection")
        return resp

    ssh_gate = asyncio.Lock()
    ssh_keys = SshKeys(ssh_home)

    async def ssh_view(detail=None):
        """Return SSH password-login status, sshd service state, and authorized keys."""
        if detail is None and await asyncio.to_thread(helper.available):
            ok, _, d = await asyncio.to_thread(helper.ssh_status)
            detail = d if ok else None
        d = detail if isinstance(detail, dict) else {}
        try:
            keys = await asyncio.to_thread(ssh_keys.list)
        except OSError:
            keys = []
        return {"password_login": d.get("password_login") if isinstance(d.get("password_login"), bool) else None,
                "ssh_active": d.get("ssh_active"), "keys": len(keys), "key_list": keys, "user": d.get("user") or "radxa"}

    async def api_ssh(request):
        """Settings SSH management: status / on / off / add_key / remove_key."""
        _, sess = session_of(request)
        if not origin_ok(request):
            return JSONResponse({"ok": False, "error": "Invalid request origin"}, status_code=403)
        if not sess:
            return JSONResponse({"ok": False, "error": "Please log in first"}, status_code=401)
        if request.headers.get("x-csrf") != sess["csrf"]:
            return JSONResponse({"ok": False, "error": "Request validation failed; refresh the page and try again"}, status_code=403)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        action = body.get("action", "status")
        if action not in ("status", "on", "off", "add_key", "remove_key"):
            return JSONResponse({"ok": False, "error": "Unknown action"}, status_code=400)
        if action in ("on", "add_key") and not auth.has_password():
            what = "enabling SSH" if action == "on" else "adding SSH keys"
            return JSONResponse({"ok": False, "error": f"Set a web login password above before {what}"}, status_code=403)
        if action == "on":
            pw, confirm = body.get("password"), body.get("confirm")
            if not isinstance(pw, str) or not 8 <= len(pw) <= 64:
                return JSONResponse({"ok": False, "error": "SSH password must be 8-64 characters"}, status_code=400)
            if pw != confirm:
                return JSONResponse({"ok": False, "error": "Inputs do not match"}, status_code=400)
        by = _client_desc(request)
        async with ssh_gate:
            if action == "status":
                return JSONResponse({"ok": True, "ssh": await ssh_view()})
            if action in ("add_key", "remove_key"):
                try:
                    if action == "add_key":
                        added, k = await asyncio.to_thread(ssh_keys.add, body.get("key"))
                    else:
                        gone = await asyncio.to_thread(ssh_keys.remove, body.get("fp"))
                except BadKey as e:
                    return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
                except OSError as e:
                    log.error("Failed to write SSH key", cat="System", raw=f"{type(e).__name__}: {e}", step=action,
                              hint="Check /home/radxa/.ssh permissions; export diagnostics if needed")
                    return JSONResponse({"ok": False, "error": f"Cannot write authorized_keys: {e}"}, status_code=500)
                if action == "add_key":
                    what = f"{k['type']} {k['comment'] or '(no comment)'} {k['fp']}"
                    log.info(f"SSH key {'added' if added else 'already present'}: {what} ({by})", cat="System")
                    return JSONResponse({"ok": True, "added": added, "fp": k["fp"], "ssh": await ssh_view()})
                if gone:
                    log.info(f"SSH key removed: {body.get('fp')} ({by})", cat="System")
                return JSONResponse({"ok": True, "removed": gone, "ssh": await ssh_view()})
            if not await asyncio.to_thread(helper.available):
                return JSONResponse({"ok": False, "error": "Root helper is not installed; cannot toggle SSH password login from web UI"},
                                    status_code=503)
            if action == "on":
                ok, result, detail = await asyncio.to_thread(helper.ssh_password, by, True, body["password"])
            else:
                ok, result, detail = await asyncio.to_thread(helper.ssh_password, by, False)
            word = "enabled" if action == "on" else "disabled"
            if not ok:
                why = helper.friendly(result, detail, "ssh-password")
                log.warn(f"SSH password login not {word}: {why}", cat="System", step=f"ssh-password {action}",
                         hint="Follow the hint above; export diagnostics if needed")
                return JSONResponse({"ok": False, "error": why},
                                    status_code=400 if result in ("bad_password", "bad_args") else 502)
            log.info(f"SSH password login {word} ({by})", cat="System")
            return JSONResponse({"ok": True, "ssh": await ssh_view()})

    async def api_logs(request):
        _, sess = session_of(request)
        if not sess:
            return JSONResponse({"ok": False}, status_code=401)
        try:
            limit = max(1, min(int(request.query_params.get("limit", 200)), 500))
        except ValueError:
            return JSONResponse({"ok": False, "error": "limit must be a number"}, status_code=400)
        return JSONResponse({"ok": True, "items": log.recent(limit=limit)})

    async def api_diag(request):
        """Diagnostics bundle: recent 2-day logs, current state, versions, and robotd/padd journals."""
        _, sess = session_of(request)
        if not sess:
            return JSONResponse({"ok": False}, status_code=401)
        log.info(f"Exported diagnostics bundle ({_client_desc(request)})", cat="System")
        # Capture state on event loop before worker thread
        snap = summarize(robotd, duck, servo, ob=onboard, bat=battery, net=net, commit=commit, auth=auth)
        raw = {"hello": robotd.hello.value, "health": robotd.health.value, "busStatus": robotd.bus.value,
               "policies": robotd.policies.value, "calibration": robotd.calib.value,
               "pad": robotd.pad.value, "system": robotd.sysinfo.value}
        if onboard is not None:
            raw["onboarding_records"] = dict(onboard.rec)
        if pad is not None:
            try:
                raw["pad_page"] = pad.diag()
            except Exception as e:  # noqa: BLE001
                raw["pad_page"] = f"error: {type(e).__name__}: {e}"
        if tune is not None:
            try:
                raw["tune"] = tune.diag()
            except Exception as e:  # noqa: BLE001
                raw["tune"] = f"error: {type(e).__name__}: {e}"
        data = await asyncio.to_thread(_diag_zip, log, snap, raw)
        name = f"studio-diag-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def api_profile(request):
        """Export duck profile for training (read-only JSON download)."""
        _, sess = session_of(request)
        if not sess:
            return JSONResponse({"ok": False}, status_code=401)
        if tune is None:
            return JSONResponse({"ok": False, "error": "Tuning backend is not enabled on this duck"}, status_code=404)
        log.info(f"Exported duck profile ({_client_desc(request)})", cat="Tuning")
        data = json.dumps(tune.profile(), ensure_ascii=False, indent=1, default=str).encode("utf-8")
        name = f"duck-profile-{time.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(data, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def sensor_read(request, kind):
        headers = {"Cache-Control": "no-store"}
        _, sess = session_of(request)
        if not sess:
            return JSONResponse({"ok": False, "error": "Log in first to view sensors"}, status_code=401, headers=headers)
        if sensors is None:
            return JSONResponse({"ok": False, "error": "Sensors backend is not enabled"}, status_code=503, headers=headers)
        try:
            data = await getattr(sensors, kind)()
            if kind == "camera" and sensor_auto is not None:
                data["auto"] = sensor_auto.view()
            return JSONResponse({"ok": True, "data": data}, headers=headers)
        except Exception as e:
            label = "Camera" if kind == "camera" else "ToF"
            log.error(f"Failed to read {label} status", cat="Sensors", raw=f"{type(e).__name__}: {e}", exc=True)
            return JSONResponse({"ok": False, "error": f"Unable to read {label} status right now; retry shortly or check logs"},
                                status_code=503, headers=headers)

    async def api_camera(request):
        return await sensor_read(request, "camera")

    async def api_tof(request):
        return await sensor_read(request, "tof")

    def model_response(payload, status=200):
        return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})

    def model_access(request, *, write=False):
        _, sess = session_of(request)
        if not sess:
            return model_response({"ok": False, "error": "Log in first to manage models"}, 401)
        if write and (not origin_ok(request) or request.headers.get("x-csrf") != sess["csrf"]):
            return model_response({"ok": False, "error": "Request verification failed; refresh and retry"}, 403)
        if write and updates.installing():
            return model_response({'ok':False,'error':'Software update in progress; wait for completion'},409)
        if write and (mapping_requests or (pad is not None and pad.mapping.blocked)):
            return model_response({'ok':False,'error':'Finish or cancel gamepad mapping before modifying models'},409)
        if models is None:
            return model_response({"ok": False, "error": "Model management is not enabled in this mode"}, 503)
        return None

    async def model_result(operation):
        try:
            return model_response({"ok": True, "data": await operation()})
        except ValueError as e:
            return model_response({"ok": False, "error": str(e), "code": getattr(e, "code", "invalid")},
                                  getattr(e, "status", 400))
        except Exception as e:
            log.error("Model management operation failed", cat="Models", raw=f"{type(e).__name__}: {e}", exc=True)
            return model_response({"ok": False, "error": "Model management error; check logs and retry"}, 500)

    async def limited_body(request, limit):
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) < 0:
                    raise ValueError()
                if int(length) > limit:
                    raise OverflowError()
            except ValueError:
                raise ValueError("Invalid content length") from None
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > limit:
                raise OverflowError()
            data.extend(chunk)
        return data

    async def api_models(request):
        refused = model_access(request)
        if refused is not None:
            return refused
        return await model_result(models.view)

    async def api_model_op(request):
        refused = model_access(request, write=True)
        if refused is not None:
            return refused
        try:
            body = json.loads(await limited_body(request, MODEL_OP_LIMIT))
            if not isinstance(body, dict):
                raise ValueError()
        except OverflowError:
            return model_response({"ok": False, "error": "Request body too large"}, 413)
        except (ValueError, UnicodeError, RecursionError):
            return model_response({"ok": False, "error": "Invalid request format"}, 400)
        task=asyncio.current_task();update_commands.add(task)
        try:
            refused=model_access(request,write=True)
            if refused is not None:return refused
            return await model_result(lambda: models.handle(body, by=_client_desc(request)))
        finally:update_commands.discard(task)

    async def api_model_import(request):
        refused = model_access(request, write=True)
        if refused is not None:
            return refused
        # Original filename is only used for extension check; temp file uses random name.
        filename = request.query_params.get("filename", "")
        if not filename or len(filename) > 200 or any(c in filename for c in ("/", "\\", ":", "\x00")):
            return model_response({"ok": False, "error": "Select a model package file"}, 400)
        # kind=action also accepts single .onnx files; reject unknown kinds.
        kind = request.query_params.get("kind") or None
        if kind not in (None, "action"):
            return model_response({"ok": False, "error": "Unknown import kind"}, 400)
        suffixes = (".tar.gz", ".tgz", ".zip") + ((".onnx",) if kind == "action" else ())
        suffix = next((s for s in suffixes if filename.lower().endswith(s)), None)
        if suffix is None:
            return model_response({"ok": False, "error": "Select a .onnx model or ZIP/tar.gz action bundle" if kind == "action"
                                   else "Select a ZIP, tar.gz, or tgz model package"}, 400)
        name = request.query_params.get("name", "")
        if len(name) > 80:
            return model_response({"ok": False, "error": "Model name must be at most 80 characters"}, 400)
        async with model_upload_gate:
            temporary = None
            try:
                refused=model_access(request,write=True)
                if refused is not None:return refused
                length = request.headers.get("content-length")
                if length is not None and int(length) > MODEL_UPLOAD_LIMIT:
                    raise OverflowError()
                upload_dir = Path(state_dir or auth.dir) / "model-uploads"
                await asyncio.to_thread(upload_dir.mkdir, parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(suffix=suffix, dir=upload_dir)
                total = 0
                with os.fdopen(fd, "wb") as output:
                    async for chunk in request.stream():
                        total += len(chunk)
                        if total > MODEL_UPLOAD_LIMIT:
                            raise OverflowError()
                        await asyncio.to_thread(output.write, chunk)
                if not total:
                    raise ValueError("Model package file is empty")
                if kind == "action":
                    return await model_result(lambda: models.import_package(Path(temporary), name=name, kind=kind))
                return await model_result(lambda: models.import_package(Path(temporary), name=name))
            except OverflowError:
                return model_response({"ok": False, "error": "Model package cannot exceed 64 MiB"}, 413)
            except ValueError as e:
                return model_response({"ok": False, "error": str(e) or "Invalid request format"}, 400)
            finally:
                if temporary is not None:
                    await asyncio.to_thread(Path(temporary).unlink, missing_ok=True)

    async def meshes(request):
        """Serve 3D meshes.bin (2.8 MB) using pre-compressed .gz when accepted."""
        raw = web_dir / "model" / "meshes.bin"
        gz = raw.with_suffix(".bin.gz")
        headers = {"Vary": "Accept-Encoding"}
        if gz.is_file() and "gzip" in request.headers.get("accept-encoding", ""):
            headers["Content-Encoding"] = "gzip"
            return WebFileResponse(gz, media_type="application/octet-stream", headers=headers)
        return WebFileResponse(raw, media_type="application/octet-stream", headers=headers)

    # ---- WebSocket ----

    def update_viewers():
        robotd.set_viewers(sum(1 for c in clients.values() if c["visible"]))
        if hasattr(robotd, "set_telemetry_viewers"):
            now = time.monotonic()
            robotd.set_telemetry_viewers(sum(1 for c in clients.values()
                if (c["visible"] and c.get("route") in ("/servos", "/imu"))
                or overview_diagnostics_active(c, now)))

    tune_err = {"logged": False}

    def tune_view():
        """Return tune card snapshot; log error once and return None on failure."""
        try:
            v = tune.view()
            tune_err["logged"] = False
            return v
        except Exception as e:  # noqa: BLE001
            if not tune_err["logged"]:
                tune_err["logged"] = True
                log.error("Failed to build tuning snapshot", cat="Tuning", raw=f"{type(e).__name__}: {e}", exc=True,
                          hint="Studio internal error; export diagnostics. Does not affect standing/walking")
            return None

    async def ws_endpoint(ws: WebSocket):
        token, sess = session_of(ws)
        if not origin_ok(ws):
            log.warn(f"Rejected WebSocket with invalid Origin ({_client_desc(ws)})", cat="Connection",
                     raw=f"Origin={ws.headers.get('origin')!r} Host={ws.headers.get('host')!r}",
                     hint="Normal Studio usage will not trigger this; cross-site connections are blocked")
            await ws.close(code=4403)
            return
        if not sess:
            log.info(f"WebSocket missing valid session; closing with 4401 ({_client_desc(ws)})", cat="Connection")
            await ws.accept()
            await ws.close(code=4401)
            return
        await ws.accept()
        by = _client_desc(ws)
        # Match http/https scheme to the incoming WebSocket scheme
        my_net = net.for_scheme("https" if ws.url.scheme == "wss" else "http") if net is not None else None
        pad_conn = pad.new_conn() if pad is not None else None
        me = {"visible": True, "by": by, "hb_at": time.monotonic(), "route": None,
              "pad_visible": None, "diagnostics_visible": False}
        clients[ws] = me
        update_viewers()
        log.info(f"Client connected ({by}, {len(clients)} total)", cat="Connection", dedup=False)
        logq = log.subscribe()
        send_lock = asyncio.Lock()

        async def send(obj):
            async with send_lock:
                await ws.send_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        loop = asyncio.get_running_loop()

        async def sender():
            await send({"t": "hello", "version": VERSION, "duck": duck})
            last_check = time.monotonic()
            last_imu = 0.0
            last_tune = 0.0
            pad_tick = 0
            pad_lists = None
            state_tick = 0
            onb_sent = None
            while True:
                quiet = time.monotonic() - me["hb_at"]
                if quiet > HB_DEAD_S:
                    # Background timer throttling must not end server-owned music.
                    # A matched native Pong renews transport liveness only: it never
                    # changes hb_at, visible viewers, drive-area state or ownership.
                    music = pad is not None and pad.motion_studio.background_music(pad_conn)
                    ping = ws.scope.get("extensions", {}).get("studio.websocket.ping")
                    transport_alive = music and time.monotonic() - me.get("pong_at", 0.) <= HB_DEAD_S
                    if music and not transport_alive and callable(ping):
                        transport_alive = await ping(MUSIC_PONG_TIMEOUT_S)
                        if transport_alive:
                            me["pong_at"] = time.monotonic()
                    if not transport_alive:
                        log.info(f"Client inactive for {HB_DEAD_S:.0f}s with no pong; closing ({by})", cat="Connection")
                        await ws.close(code=4408)
                        return
                if me["visible"] and quiet > HB_QUIET_S:
                    me["visible"] = False
                    update_viewers()
                if me["visible"]:
                    st = summarize(robotd, duck, servo, ob=onboard, bat=battery, net=my_net, commit=commit, auth=auth)
                    # Omit unchanged onboarding payload except once per second
                    state_tick += 1
                    onb = st.get("onboarding")
                    if onb is not None and onb == onb_sent and state_tick % STATE_HZ != 0:
                        del st["onboarding"]
                        st["onboarding_same"] = True
                    else:
                        onb_sent = onb
                    await send(st)
                    diagnostics_active = overview_diagnostics_active(me)
                    if servo is not None and (me.get("route") == "/servos" or diagnostics_active
                            or (servo.state in ("open", "opening") and me.get("route") not in ("/", "/pad"))):
                        # Push full bus snapshot when taken over; otherwise read-only view from robotd
                        await send(servo.snapshot(diagnostics=diagnostics_active) if servo.state == "open"
                                   else view_from_robotd(robotd, servo.directions, servo.state,
                                                         servo.error, servo.sim))
                    imu_live = me.get("route") == "/imu" or diagnostics_active
                    if imu is not None and (imu_live or time.monotonic() - last_imu >= .5):
                        last_imu = time.monotonic()        # Live views share the state tick and use only the owner cache.
                        await send(imu.view())
                    if (tune is not None and time.monotonic() - last_tune >= 1.0
                            and me.get("route") in ("/", "/pad", "/settings")):
                        last_tune = time.monotonic()
                        tv = tune_view()
                        if tv is not None:
                            await send(tv)
                    if pad is not None:
                        # Push gamepad snapshot at 10 Hz when pad_visible, else 1 Hz
                        pad_tick += 1
                        hot = me["pad_visible"] if me.get("pad_visible") is not None else me.get("route") == "/pad"
                        full = pad_tick % STATE_HZ == 0
                        if hot or full:
                            try:
                                snap = pad.safe_snapshot(conn=pad_conn)
                            except Exception:  # noqa: BLE001
                                snap = None
                            if snap is not None:
                                m = snap.get("motion") if isinstance(snap.get("motion"), dict) else None
                                if m is not None:
                                    lists = {k: m.get(k) for k in PAD_HEAVY}
                                    if not full and lists == pad_lists:
                                        snap = dict(snap, motion={k: v for k, v in m.items() if k not in PAD_HEAVY},
                                                    lists_same=True)
                                    else:
                                        pad_lists = lists
                                await send(snap)
                    await asyncio.sleep(1 / STATE_HZ)
                else:
                    await send({"t": "hb"})
                    await asyncio.sleep(1.0)
                if time.monotonic() - last_check > 5:
                    last_check = time.monotonic()
                    # Check session without extending idle timeout
                    if session_of(ws, touch=False)[1] is None:
                        await ws.close(code=4401)
                        return

        async def log_forwarder():
            while True:
                entry = await logq.get()
                await send({"t": "log", "entry": entry})

        cmd_tasks = set()

        async def run_relax(cid):
            if pad is not None:
                pad.note_relax(by)
            try:
                if servo is not None and servo.state == "open":
                    # During servo tuning robotd is paused: disable torque directly on bus
                    accepted, reason = await asyncio.to_thread(servo.relax_all, by)
                elif servo is not None and servo.orphan and robotd.present is not True:
                    log.warn("E-Stop: previous servo session was open and could not be resumed", cat="Safety", raw=f"by={by}",
                             hint="Open the Servos page to take over the bus and disable torque, or power off")
                    accepted, reason = False, "Previous servo session may still be holding torque: go to Servos page to take over bus (or power off)"
                else:
                    accepted, reason = await robotd.relax(by)
            except Exception as e:  # noqa: BLE001  # Unexpected error; still send a receipt to the UI
                log.error("Studio error handling E-Stop command", cat="Command", raw=f"{type(e).__name__}: {e}", exc=True,
                          hint="Studio internal error; use gamepad Select or power off, and export diagnostics")
                accepted, reason = False, f"Studio error: {type(e).__name__}"
            try:
                await send({"t": "result", "id": cid, "ok": accepted, "reason": reason})
            # Client disconnected; E-Stop already ran
                pass

        async def run_servo(name, cid, args=None):
            if servo is None:
                await send({"t": "result", "id": cid, "ok": False, "reason": "Servo bus is not configured on this duck"})
                return
            async with servo_gate:
                if name == "servo.open":
                    # Auto-takeover on entering Servos page; refuse while walking/policy/homing
                    pose = summarize(robotd, duck, net=net, auth=auth)["posture"] if servo.state != "open" else None
                    if pose in ("walking", "policy", "homing"):
                        ok, why = False, "Duck is walking or policy is active: stop or sit down before taking over Servos (read-only for now)"
                    else:
                        was_open = servo.state == "open"
                        if was_open:
                            # Already open
                            ok, why = await asyncio.to_thread(servo.open, by, (args or {}).get("port"))
                        else:
                            robotd.set_paused(True, "Servo tuning")
                            taken, take_why = await asyncio.to_thread(_bus_take, by, servo)
                            if not taken:
                                ok, why = False, take_why
                                if helper.flag_present():
                                    why = f"{why}. robotd is blocked by takeover flag: resolve and reconnect, or reboot the duck"
                                robotd.set_paused(False)
                            else:
                                ok, why = await asyncio.to_thread(servo.open, by, (args or {}).get("port"))
                                if not ok:
                                    robotd.set_paused(False)
                                    # robotd stopped but bus failed to open: restore robotd
                                    asyncio.create_task(asyncio.to_thread(_bus_give_back, by, servo))
                                else:
                                    servo.orphan = servo.conflict_at = None
                                    servo.handed_back_at = None
                                    servo.handback_note = servo.giveback_error = servo.giveback_hint = None
                                    await remember_marker(by)
                elif (args or {}).get("if_idle"):
                    done, kept = await leave_servos(f"Left Servos page ({by})", ws, by)
                    if done:
                        ok, why = True, None
                    else:
                        log.info(f"Left Servos page: {kept}; keeping bus open (click 'Hand Back to robotd' to release)", cat="Bus",
                                 raw=f"by={by}", dedup=False)
                        ok, why = True, f"{kept}: kept open"
                else:
                    ok, why = await hand_back(f"Clicked 'Hand Back to robotd' ({by})", by)
            try:
                await send({"t": "result", "id": cid, "ok": ok, "reason": why})
            except Exception:  # noqa: BLE001
                pass

        async def run_servo_op(cid, args):  # noqa: C901
            """Servos page operations (torque, sliders, motion, calibration, scan, registers) in worker thread."""
            from .servo_ops import Refused
            data, ok, reason = None, True, None
            ops = servo.ops if servo is not None else None
            if ops is None:
                ok, reason = False, "Servo bus is not taken over: refresh Servos page to retry"
            else:
                # Track in-flight op on event loop before worker thread dispatch
                try:
                    tok = servo.op_begin(args.get("op"))
                except Refused as e:
                    ok, reason, tok = False, str(e), None
                if tok is not None:
                    try:
                        data = await asyncio.to_thread(ops.handle, args, tok)
                    except Refused as e:
                        ok, reason = False, str(e)
                    except Exception as e:  # noqa: BLE001
                        ok, reason = False, f"Studio error: {type(e).__name__}: {e}"
                        log.error(f"Servo operation error: {args.get('op')}", cat="Bus", raw=f"{type(e).__name__}: {e}", exc=True,
                                  hint="Studio internal error; export diagnostics")
                    finally:
                        servo.op_end(tok)
            try:
                await send({"t": "result", "id": cid, "ok": ok, "reason": reason, "data": data})
            except Exception:  # noqa: BLE001
                pass

        async def run_imu_op(cid, args):
            """IMU page operations."""
            from .imu import Refused as ImuRefused
            data, ok, reason = None, True, None
            if imu is None:
                ok, reason = False, "IMU backend is not enabled"
            else:
                try:
                    data = await imu.handle(args, by)
                except ImuRefused as e:
                    ok, reason = False, str(e)
                except Exception as e:  # noqa: BLE001
                    ok, reason = False, f"Studio error: {type(e).__name__}: {e}"
                    log.error(f"IMU operation error: {args.get('op')}", cat="IMU", raw=f"{type(e).__name__}: {e}", exc=True,
                              hint="Studio internal error; export diagnostics")
            try:
                await send({"t": "result", "id": cid, "ok": ok, "reason": reason, "data": data})
            except Exception:  # noqa: BLE001
                pass

        async def run_calib_op(cid, args):
            """Onboarding Step 3: preview (dry_run) and commit calibration to robotd."""
            from .commit import Refused as CommitRefused
            op = args.get("op") if isinstance(args, dict) else None
            if commit is None:
                await send({"t": "result", "id": cid, "ok": False, "reason": "Commit calibration is not enabled on this duck"})
                return
            if op == "preview":
                try:
                    data = await asyncio.to_thread(commit.preview, by)
                except CommitRefused as e:
                    await send({"t": "result", "id": cid, "ok": False, "reason": str(e)})
                    return
                except Exception as e:  # noqa: BLE001
                    log.error("Failed to compute commit preview", cat="Calibration", raw=f"{type(e).__name__}: {e}", exc=True,
                              hint="Studio internal error; export diagnostics")
                    await send({"t": "result", "id": cid, "ok": False,
                                "reason": f"Studio error: {type(e).__name__}: {e}"})
                    return
                await send({"t": "result", "id": cid, "ok": True, "data": data})
                return
            if op != "commit":
                await send({"t": "result", "id": cid, "ok": False, "reason": f"Unknown commit op: {op}"})
                return
            try:
                view = commit.view(robotd, servo)
            except Exception as e:  # noqa: BLE001
                log.error("Failed to check commit readiness", cat="Calibration", raw=f"{type(e).__name__}: {e}", exc=True,
                          hint="Studio internal error; export diagnostics")
                await send({"t": "result", "id": cid, "ok": False, "reason": f"Studio error: {type(e).__name__}: {e}"})
                return
            if not view.get("ready"):
                missing = [i for i in view.get("items") or [] if not i.get("ok")]
                reason = "Not ready: " + "; ".join(f"{i.get('label')}: {i.get('text')}" for i in missing[:3])
                if len(missing) > 3:
                    reason += "…"
                await send({"t": "result", "id": cid, "ok": False, "reason": reason, "data": {"readiness": view}})
                return
            await send({"t": "commit", "phase": "started", "msg": "Writing to robotd: robotd will restart (~20s) and disable torque first"})
            await send({"t": "result", "id": cid, "ok": True, "data": {"started": True, "readiness": view}})

            def progress(msg, detail=None):
                item = {"t": "commit", "phase": "running", "msg": msg}
                if isinstance(detail, dict):
                    item["detail"] = detail
                try:
                    asyncio.run_coroutine_threadsafe(send(item), loop)
                except Exception:  # noqa: BLE001
                    pass

            try:
                res = await asyncio.to_thread(commit.commit, by, progress)
            except Exception as e:  # noqa: BLE001
                log.error("Commit background task failed", cat="Calibration", raw=f"{type(e).__name__}: {e}", exc=True,
                          hint="Check whether robotd restarted and whether previous files were restored")
                res = {"ok": False, "reason": f"Studio error: {type(e).__name__}: {e}", "detail": {}}
            await send({"t": "commit", "phase": "done" if res.get("ok") else "failed", "ok": bool(res.get("ok")),
                        "reason": res.get("reason"), "detail": res.get("detail") or {}})

        async def run_tune_op(cid, args):
            """Tuning card / body mass operations."""
            from .tune import Refused as TuneRefused
            data, ok, reason = None, True, None
            if tune is None:
                ok, reason = False, "Tuning backend is not enabled"
            else:
                try:
                    data = await tune.handle(args, by)
                except TuneRefused as e:
                    ok, reason = False, str(e)
                except Exception as e:  # noqa: BLE001
                    ok, reason = False, f"Studio error: {type(e).__name__}: {e}"
                    log.error(f"Tuning operation error: {args.get('op')}", cat="Tuning", raw=f"{type(e).__name__}: {e}", exc=True,
                              hint="Studio internal error; export diagnostics")
            try:
                await send({"t": "result", "id": cid, "ok": ok, "reason": reason, "data": data})
            except Exception:  # noqa: BLE001
                pass

        async def run_pad_op(cid, args):
            """Gamepad page operations dispatched to thread pool."""
            data, ok, reason = None, True, None
            if pad is None:
                ok, reason = False, "Gamepad backend is not enabled (bench mode)"
            else:
                try:
                    data = await asyncio.get_running_loop().run_in_executor(
                        pad.executor_for(args), lambda: pad.handle(args, by, pad_conn))
                except PadRefused as e:
                    ok, reason = False, str(e)
                except Exception as e:  # noqa: BLE001
                    ok, reason = False, f"Studio error: {type(e).__name__}: {e}"
                    log.error(f"Gamepad operation error: {args.get('op')}", cat="Gamepad", raw=f"{type(e).__name__}: {e}", exc=True,
                              hint="Studio internal error; export diagnostics")
            try:
                await send({"t": "result", "id": cid, "ok": ok, "reason": reason, "data": data})
            except Exception:  # noqa: BLE001
                pass

        async def onboard_op(cid, args):
            """Onboarding checklist actions (persisted in Studio only)."""
            op = args.get("op")
            if onboard is None:
                return {"t": "result", "id": cid, "ok": False, "reason": "Onboarding checklist is not enabled"}
            what = {"confirm_servo": "confirm servo", "done": "mark done", "skip": "skip", "unskip": "unskip",
                    "undone": "unmark done"}.get(op)
            if what is None:
                return {"t": "result", "id": cid, "ok": False, "reason": f"Unknown command: {op}"})
            step = str(args.get("step") or "")
            confirmed = args.get("confirmed") is True
            if op == "confirm_servo":
                ok, why = onboard.confirm_servo(robotd, by)
            elif op in ("done", "skip"):
                ok, why = onboard.mark(step, op, robotd, pad, servo, by, confirmed)
            else:
                ok, why = onboard.unmark(step, "skip" if op == "unskip" else "done", by, servo, confirmed)
            if ok:
                onboard.tick(robotd, pad, servo)
                await asyncio.to_thread(onboard.save)
            else:
                log.info(f"Onboarding {what} refused: {why}", cat="Calibration", step=step or "Onboarding")
            return {"t": "result", "id": cid, "ok": ok, "reason": why}

        async def run_battery_op(cid, args):
            """Settings Battery section: get / set / calibrate."""
            data, ok, reason = None, True, None
            if battery is None:
                ok, reason = False, "Battery estimator is not enabled"
            else:
                try:
                    data = await battery.handle(args, by)
                except ValueError as e:
                    ok, reason = False, str(e)
                except Exception as e:  # noqa: BLE001
                    ok, reason = False, f"Studio error: {type(e).__name__}: {e}"
                    log.error(f"Battery settings error: {args.get('op')}", cat="Battery", raw=f"{type(e).__name__}: {e}", exc=True,
                              hint="Studio internal error; export diagnostics")
            try:
                await send({"t": "result", "id": cid, "ok": ok, "reason": reason, "data": data})
            except Exception:  # noqa: BLE001
                pass

        async def run_net_op(cid, args):
            """Wi-Fi provisioning: scan / connect / forget / hotspot."""
            from .net import Refused as NetRefused, validate_connect
            if net is None:
                await send({"t": "result", "id": cid, "ok": False, "reason": "Network provisioning is not enabled"})
                return
            op = args.get("op")
            if op == "scan":
                try:
                    data = await net.scan()
                except NetRefused as e:
                    await send({"t": "result", "id": cid, "ok": False, "reason": str(e)})
                    return
                except Exception as e:  # noqa: BLE001
                    log.error("Wi-Fi scan error", cat="Network", raw=f"{type(e).__name__}: {e}", exc=True,
                              hint="Studio internal error; export diagnostics")
                    await send({"t": "result", "id": cid, "ok": False, "reason": f"Studio error: {type(e).__name__}: {e}"})
                    return
                await send({"t": "result", "id": cid, "ok": True, "data": data})
                return
            if op == "connect":
                try:
                    ssid, psk = validate_connect(args.get("ssid"), args.get("psk"))
                    started = net.begin_connect()
                except NetRefused as e:
                    await send({"t": "result", "id": cid, "ok": False, "reason": str(e)})
                    return
                if not started:
                    await send({"t": "result", "id": cid, "ok": False, "reason": "Connection in progress; wait a moment"})
                    return
                await send({"t": "result", "id": cid, "ok": True, "reason": "Switching network",
                            "data": {"ssid": ssid}})

                async def do_connect():
                    try:
                        await net.connect(ssid, psk, by)
                    except Exception as e:  # noqa: BLE001
                        net.busy = None
                        log.error("Wi-Fi connect background task error", cat="Network", raw=f"{type(e).__name__}: {e}", exc=True,
                                  hint="Studio internal error; export diagnostics")
                # Stored on net so it survives client disconnect when hotspot drops
                net.connect_task = asyncio.create_task(do_connect())
                return
            if op == "forget":
                try:
                    data = await net.forget(args.get("ssid"))
                except NetRefused as e:
                    await send({"t": "result", "id": cid, "ok": False, "reason": str(e)})
                    return
                except Exception as e:  # noqa: BLE001
                    log.error("Forget Wi-Fi error", cat="Network", raw=f"{type(e).__name__}: {e}", exc=True,
                              hint="Studio internal error; export diagnostics")
                    await send({"t": "result", "id": cid, "ok": False, "reason": f"Studio error: {type(e).__name__}: {e}"})
                    return
                await send({"t": "result", "id": cid, "ok": True, "data": data})
                return
            if op in ("hotspot-up", "hotspot-down"):
                try:
                    started = net.begin_hotspot()
                except NetRefused as e:
                    await send({"t": "result", "id": cid, "ok": False, "reason": str(e)})
                    return
                if not started:
                    await send({"t": "result", "id": cid, "ok": False, "reason": "Wi-Fi operation in progress; wait a moment"})
                    return
                what = "enable hotspot" if op == "hotspot-up" else "disable hotspot"
                await send({"t": "result", "id": cid, "ok": True, "reason": f"Starting {what}"})
                ssid = args.get("ssid") if isinstance(args.get("ssid"), str) else None

                async def do_hotspot():
                    try:
                        if op == "hotspot-up":
                            ok, reason = await net.hotspot_up(by)
                        else:
                            ok, reason = await net.hotspot_down(by, ssid)
                        if not ok:
                            log.info(f"Failed to {what}: {reason}", cat="Network", step=op)
                    except Exception as e:  # noqa: BLE001
                        net.busy = None
                        log.error(f"Hotspot background task error ({what})", cat="Network", raw=f"{type(e).__name__}: {e}", exc=True,
                                  hint="Studio internal error; export diagnostics")
                # Survives client disconnect
                net.connect_task = asyncio.create_task(do_hotspot())
                return
            await send({"t": "result", "id": cid, "ok": False, "reason": f"Unknown network op: {op}"})

        async def run_onboard_op(cid, args):
            try:
                out = await onboard_op(cid, args)
            except Exception as e:  # noqa: BLE001
                log.error("Onboarding action error", cat="System", raw=f"{type(e).__name__}: {e}", exc=True,
                          hint="Studio internal error; export diagnostics")
                out = {"t": "result", "id": cid, "ok": False, "reason": f"Studio error: {type(e).__name__}"}
            try:
                await send(out)
            except Exception:  # noqa: BLE001
                pass

        async def receiver():
            while True:
                msg = await ws.receive_json()
                me["hb_at"] = time.monotonic()
                if not isinstance(msg, dict):
                    continue
                t = msg.get("t")
                if t == "hb":
                    me["route"] = msg.get("route")
                    me["diagnostics_visible"] = msg.get("diagnostics_visible") is True
                    if isinstance(msg.get("pad_visible"), bool):
                        me["pad_visible"] = msg["pad_visible"]
                    vis = bool(msg.get("visible", True))
                    if vis != me["visible"]:
                        me["visible"] = vis
                    update_viewers()
                elif t == "cmd":
                    name, cid = msg.get("name"), msg.get("id")
                    args = msg.get('args') or {}
                    if updates.installing() and not safe_during_update(name,args):
                        await send({'t':'result','id':cid,'ok':False,'reason':'Software update in progress; wait for completion (E-Stop still available)'})
                        continue
                    if (imu is not None and getattr(imu, 'blocks_updates', lambda: False)()
                            and name in {'pad.op', 'tune.op', 'servo.open', 'servo.op', 'calib.op'}
                            and not safe_during_update(name, args)):
                        await send({'t':'result','id':cid,'ok':False,
                                    'reason':'IMU calibration / chip switch session active: verify or cancel first (E-Stop still available)'})
                        continue
                    if ((mapping_requests or (pad is not None and getattr(getattr(pad, 'mapping', None), 'blocked', False)))
                            and name in {'tune.op', 'servo.open', 'servo.op', 'calib.op', 'imu.op'}
                            and not safe_during_update(name, args)):
                        await send({'t':'result','id':cid,'ok':False,
                                    'reason':'Finish or cancel gamepad mapping first (web E-Stop still available)'})
                        continue
                    if name not in COMMANDS:
                        await send({"t": "result", "id": cid, "ok": False, "reason": f"Unsupported command: {name}"})
                        continue
                    if name == "imu.op":
                        task = asyncio.create_task(run_imu_op(cid, msg.get("args") or {}))
                        cmd_tasks.add(task)
                        update_commands.add(task)
                        task.add_done_callback(update_commands.discard)
                        task.add_done_callback(cmd_tasks.discard)
                        continue
                    if name == "calib.op":
                        task = asyncio.create_task(run_calib_op(cid, msg.get("args") or {}))
                        cmd_tasks.add(task)
                        update_commands.add(task)
                        task.add_done_callback(update_commands.discard)
                        task.add_done_callback(cmd_tasks.discard)
                        continue
                    if name == "tune.op":
                        task = asyncio.create_task(run_tune_op(cid, msg.get("args") or {}))
                        cmd_tasks.add(task)
                        update_commands.add(task)
                        task.add_done_callback(update_commands.discard)
                        task.add_done_callback(cmd_tasks.discard)
                        continue
                    if name == "pad.op":
                        task = asyncio.create_task(run_pad_op(cid, msg.get("args") or {}))
                        if args.get('op')=='mapping' and args.get('action')=='begin':
                            # Reserve on the event loop before another command
                            # from the same WebSocket packet can start takeover.
                            mapping_requests.add(task)
                            task.add_done_callback(mapping_requests.discard)
                        cmd_tasks.add(task)
                        update_commands.add(task)
                        task.add_done_callback(update_commands.discard)
                        task.add_done_callback(cmd_tasks.discard)
                        continue
                    if name == "onboard.op":
                        # Do not await here so disk I/O cannot block E-Stop
                        task = asyncio.create_task(run_onboard_op(cid, msg.get("args") or {}))
                        cmd_tasks.add(task)
                        update_commands.add(task)
                        task.add_done_callback(update_commands.discard)
                        task.add_done_callback(cmd_tasks.discard)
                        continue
                    if name == "net.op":
                        # Run in background task so it never blocks E-Stop
                        task = asyncio.create_task(run_net_op(cid, msg.get("args") or {}))
                        cmd_tasks.add(task)
                        update_commands.add(task)
                        task.add_done_callback(update_commands.discard)
                        task.add_done_callback(cmd_tasks.discard)
                        continue
                    if name == "servo.op":
                        task = asyncio.create_task(run_servo_op(cid, msg.get("args") or {}))
                        cmd_tasks.add(task)
                        update_commands.add(task)
                        task.add_done_callback(update_commands.discard)
                        task.add_done_callback(cmd_tasks.discard)
                        continue
                    if name == "servo.ports":
                        from .servo import list_ports
                        ports = await asyncio.to_thread(list_ports)
                        await send({"t": "result", "id": cid, "ok": True, "data": {
                            "ports": ports, "port": servo.port if servo else None, "sim": bool(servo and servo.sim),
                            "state": servo.state if servo else None}})
                        continue
                    if name in ("servo.open", "servo.close"):
                        task = asyncio.create_task(run_servo(name, cid, msg.get("args") or {}))
                        cmd_tasks.add(task)
                        update_commands.add(task)
                        task.add_done_callback(update_commands.discard)
                        task.add_done_callback(cmd_tasks.discard)
                        continue
                    if name == "battery.op":
                        task = asyncio.create_task(run_battery_op(cid, msg.get("args") or {}))
                        cmd_tasks.add(task)
                        update_commands.add(task)
                        task.add_done_callback(update_commands.discard)
                        task.add_done_callback(cmd_tasks.discard)
                        continue
                    if name == "relax":
                        # Run in background task so E-Stop is never queued
                        task = asyncio.create_task(run_relax(cid))
                        cmd_tasks.add(task)
                        update_commands.add(task)
                        task.add_done_callback(update_commands.discard)
                        task.add_done_callback(cmd_tasks.discard)
                        continue
                    log.error(f"Command '{name}' in COMMANDS has no handler", cat="System", hint="Studio bug; export diagnostics")
                    await send({"t": "result", "id": cid, "ok": False, "reason": f"Unhandled command: {name}"})

        tasks = [asyncio.create_task(f()) for f in (sender, log_forwarder, receiver)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, RuntimeError)):
                    log.warn("WebSocket connection error", cat="Connection", raw=f"{type(exc).__name__}: {exc}")
        finally:
            for t in tasks:
                t.cancel()
            verification = getattr(imu, "home_verification_task", None)
            if verification in cmd_tasks:
                verification.cancel()
            log.unsubscribe(logq)
            clients.pop(ws, None)
            if pad is not None:
                pad.mark_gone(pad_conn)
                asyncio.get_running_loop().run_in_executor(None, pad.drop_client, pad_conn, by)
            update_viewers()
            log.info(f"Client disconnected ({by}, {len(clients)} remaining)", cat="Connection", dedup=False)

    # ---- Servo Session: marker file, handback, leave, robotd conflict probe ----

    def marker_write(by):
        """Atomically write servo-session.json on bus open (in worker thread)."""
        if marker is None:
            return True
        data = {"v": 1, "port": servo.port, "sim": bool(servo.sim), "at": time.time(), "by": by,
                "boot_id": boot_id()}
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            tmp = marker.with_name(marker.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, marker)
            return True
        except OSError as e:
            log.warn("Failed to write servo session marker", cat="Bus", step=str(marker), raw=f"{type(e).__name__}: {e}",
                     hint="Check state directory disk space and permissions")
            return False

    async def remember_marker(by):
        """Write session marker and record any error on servo.marker_error."""
        ok = await asyncio.to_thread(marker_write, by)
        servo.marker_error = None if ok else (
            "Failed to write session marker: do not restart Studio; click 'Hand Back to robotd' when done")
        return ok

    def marker_drop():
        if marker is None:
            return
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warn("Failed to remove servo session marker", cat="Bus", step=str(marker), raw=f"{type(e).__name__}: {e}",
                     hint="Check state directory permissions")

    def marker_read():
        """Return (exists, parsed_dict_or_None)."""
        if marker is None:
            return False, None
        try:
            raw = marker.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False, None
        except OSError:
            return True, None
        try:
            d = json.loads(raw)
        except ValueError:
            return True, None
        return True, d if isinstance(d, dict) else None

    async def robotd_up():
        """Return True if robotd socket accepts connection (bench mode has no robotd)."""
        addr = getattr(robotd, "addr", "none")
        if not addr or addr == "none":
            return False
        try:
            _, w = await asyncio.wait_for(open_conn(addr), 1.0)
        except Exception:  # noqa: BLE001
            return False
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass
        return True

    async def handed_back(why, by=None):
        """After bus is relaxed & closed: resume robotd polling, restart robotd, update marker, and log."""
        servo.handed_back_at = time.monotonic()
        servo.orphan = None
        servo.marker_error = None
        servo.giveback_error = servo.giveback_hint = None
        robotd.set_paused(False)
        log.info(f"Handing back to robotd: {why}", cat="Bus", dedup=False)
        ok, gb_why = await asyncio.to_thread(_bus_give_back, by or "Studio", servo)
        if ok:
            await asyncio.to_thread(marker_drop)
            if servo.helper_available is False:
                log.warn("Handed back; root helper unavailable — start robotd manually: sudo systemctl start robotd", cat="Bus", dedup=False)
            else:
                log.info("robotd is back up", cat="Bus", dedup=False)
        else:
            servo.giveback_error = gb_why
            flag = await asyncio.to_thread(helper.flag_present)
            if flag:
                servo.giveback_hint = ("Takeover flag still present: manual start of robotd is blocked. Re-enter Servos and click Hand Back, or reboot the duck")
            elif getattr(servo, "giveback_stuck", False):
                servo.giveback_hint = "robotd is replying but stuck in startup phases: check Logs page for robotd messages"
                await asyncio.to_thread(marker_drop)
            else:
                servo.giveback_hint = "Run sudo systemctl start robotd manually, or re-enter Servos and hand back"
                await asyncio.to_thread(marker_drop)
            log.error(f"robotd failed to start: {gb_why}", cat="Bus", dedup=False, hint=servo.giveback_hint)

    async def hand_back(why, by=None):
        """Handle 'Hand Back to robotd': wait in-flight ops, relax & readback, close bus, restart robotd."""
        if servo is None or servo.state != "open":
            return True, None
        ok, reason = await asyncio.to_thread(servo.hand_back_bus, why, before_close=lambda: robotd.set_paused(False))
        if not ok:
            log.error("Handback to robotd refused", cat="Bus", raw=reason, hint="Wait for calibration/motion to finish and retry", dedup=False)
            return False, reason
        servo.handback_note = reason
        await handed_back(why, by)
        return True, None

    async def leave_servos(why, ws=None, by=None):
        """Leave Servos page / idle check: keep bus open if viewers/busy/holding; otherwise relax, close, and restart robotd."""
        others = sum(1 for w, c in clients.items() if w is not ws and c.get("route") == "/servos")
        if others:
            return False, f"{others} other tab(s) still on Servos page"
        verdict, info = await asyncio.to_thread(servo.leave, why, before_close=lambda: robotd.set_paused(False))
        if verdict == "handed_back":
            await handed_back(why, by)
            return True, None
        if verdict == "closed":
            return True, None
        return False, info["why"]

    async def robotd_conflict():
        """robotd was started externally during servo session: close bus without writing and resume polling."""
        async with servo_gate:
            if servo.state != "open" or servo.closing:
                return
            await asyncio.to_thread(servo.close)
            servo.conflict_at = time.monotonic()
            await asyncio.to_thread(marker_drop)
            servo.marker_error = None
            robotd.set_paused(False)
        log.error("robotd started externally during servo session (serial conflict)", cat="Bus", step="Probe robotd socket",
                  hint="Studio closed the servo bus. Always click 'Hand Back to robotd' instead of starting robotd manually", dedup=False)

    async def servo_restore():
        """On Studio startup: if servo-session.json exists and robotd is stopped, resume read-only bus takeover."""
        if servo is None:
            return
        if updates.installing():
            log.warn('robotd update guard active: pausing servo session auto-restore',cat='Bus')
            return
        there, data = await asyncio.to_thread(marker_read)
        if not there:
            return
        if data is None:
            log.warn("Corrupted servo session marker: treating as open with default port", cat="Bus", step=str(marker),
                     hint="Likely power loss during write")
            data = {}
        here = boot_id()
        if isinstance(data, dict) and data.get("boot_id") and here and data["boot_id"] != here:
            log.info("Stale servo session marker from previous boot; discarding", cat="Bus", dedup=False)
            await asyncio.to_thread(marker_drop)
            return
        if await robotd_up():
            log.info("Previous servo session marker found, but robotd is running; discarding", cat="Bus",
                     dedup=False)
            await asyncio.to_thread(marker_drop)
            return
        port = "sim" if data.get("sim") is True else (data.get("port") if isinstance(data.get("port"), str) else None)
        async with servo_gate:
            if servo.state == "open":
                return
            robotd.set_paused(True, "Servo tuning (resumed)")
            taken, take_why = await asyncio.to_thread(_bus_take, "Studio startup resume", servo)
            if not taken:
                ok, why = False, take_why
                servo.orphan = {"why": take_why, "at": time.monotonic()}
                robotd.set_paused(False)
                asyncio.create_task(asyncio.to_thread(_bus_give_back, "Studio startup resume", servo))
            else:
                ok, why = await asyncio.to_thread(servo.open, "Studio startup resume", port, True)
                if ok:
                    servo.orphan = None
                    servo.handed_back_at = None
                    servo.handback_note = servo.giveback_error = servo.giveback_hint = None
                    await remember_marker(data.get("by") if isinstance(data.get("by"), str) else "Studio startup resume")
                else:
                    servo.orphan = {"why": why, "at": time.monotonic()}
                    robotd.set_paused(False)
                    asyncio.create_task(asyncio.to_thread(_bus_give_back, "Studio startup resume", servo))
        if ok:
            log.info(f"Resumed previous servo session (read-only; {servo.holding()} servo(s) holding torque)",
                     cat="Bus", dedup=False)
        else:
            log.error("Failed to resume previous servo session; servos may still hold torque", cat="Bus", raw=why, dedup=False,
                      hint="Go to Servos page to take over and disable torque, or power off")

    async def servo_watch():
        """1 Hz servo session watchdog: auto-handback when idle & unlatched, probe robotd conflicts, clear handback banner."""
        since = None
        last_probe = last_try = 0.0
        while True:
            await asyncio.sleep(1.0)
            if servo is None:
                continue
            now = time.monotonic()
            if servo.state != "open":
                since = None
                fresh = robotd.present is True and robotd.bus.at is not None
                if servo.handed_back_at is not None and fresh and robotd.bus.at > servo.handed_back_at and not robotd.paused:
                    servo.handed_back_at = None
                    servo.handback_note = None
                    servo.giveback_error = servo.giveback_hint = None
                if servo.orphan and fresh and robotd.bus.at > servo.orphan["at"] and not robotd.paused:
                    servo.orphan = None
                    await asyncio.to_thread(marker_drop)
                    log.info("Previous servo session cleared because robotd is now running", cat="Bus", dedup=False)
                continue
            probe = servo.probe_robotd if servo.probe_robotd is not None else not servo.sim
            if probe and now - last_probe >= ROBOTD_PROBE_S and not servo.closing:
                last_probe = now
                if await robotd_up():
                    await robotd_conflict()
                    continue
            here = sum(1 for c in clients.values() if c.get("route") == "/servos")
            if here:
                since = None
            elif since is None:
                since = now
            elif now - since > SERVO_IDLE_S and now - last_try >= 5.0:
                last_try = now
                async with servo_gate:
                    if servo.state != "open":
                        continue
                    done, kept = await leave_servos(f"Servos page idle for {SERVO_IDLE_S:.0f}s with no holding torque", by="Studio Auto-Handback")
                if done:
                    since = None
                else:
                    log.warn(f"Servos page idle, but {kept}: keeping bus open", cat="Bus", raw=kept,
                             hint="Click 'Hand Back to robotd' in the top bar when ready to release torque")

    async def pad_feed():
        """Feed robotd view to PadOps at 10 Hz."""
        while True:
            await asyncio.sleep(1 / STATE_HZ)
            try:
                # A server-owned music lease consumes real robot.state frames
                # even after the last browser viewer leaves the drive area.
                robotd.set_background_music(pad.motion_studio.background_music())
                pad.feed_robot(robot_view(robotd))
            except Exception as e:  # noqa: BLE001
                log.error("Failed to feed robotd status to gamepad backend", cat="Gamepad", raw=f"{type(e).__name__}: {e}", exc=True,
                          hint="Studio internal error; export diagnostics")
                await asyncio.sleep(1.0)

    async def onboard_loop():
        """Evaluate onboarding checklist & board thermal hysteresis at 2 Hz."""
        while True:
            await asyncio.sleep(ONBOARD_TICK_S)
            try:
                # Run file I/O in worker thread so SD card stalls cannot block E-Stop
                await asyncio.to_thread(onboard.io, onboard.need_board(robotd))
                if pad is not None:
                    pad.battery_shutdown = onboard.battery_shutdown
                if onboard.tick(robotd, pad, servo):
                    await asyncio.to_thread(onboard.save)
            except Exception as e:  # noqa: BLE001
                log.error("Onboarding tick error", cat="System", raw=f"{type(e).__name__}: {e}", exc=True,
                          hint="Studio internal error; export diagnostics")
                await asyncio.sleep(5.0)

    async def sensors_on_boot():
        try:
            await sensor_auto.run_on_boot("Boot auto-detect")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.error("Boot sensor auto-configuration error", cat="Sensors", raw=f"{type(e).__name__}: {e}", exc=True,
                      hint="Does not affect standing/walking; Camera/ToF pages keep existing settings")

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await updates.start_readiness(update_readiness)
        auto_task = asyncio.create_task(sensors_on_boot()) if sensor_auto is not None else None
        watch = asyncio.create_task(servo_watch())
        feed = asyncio.create_task(pad_feed()) if pad is not None else None
        ob_task = asyncio.create_task(onboard_loop()) if onboard is not None else None
        bat_task = asyncio.create_task(battery.loop()) if battery is not None else None
        tune_task = asyncio.create_task(tune.loop()) if tune is not None else None
        robotd.start()
        if net is not None:
            net.start()
        restore = asyncio.create_task(servo_restore())
        if phone_bridge is not None:
            # Warm up ASR model in background
            asr_warm = asyncio.create_task(asyncio.to_thread(phone_bridge.warmup))
        log.info(f"Studio {VERSION} started · Python {platform.python_version()} · {platform.node()}", cat="System",
                 dedup=False)
        yield
        await updates.stop_readiness()
        if auto_task is not None:
            auto_task.cancel()
            await asyncio.gather(auto_task, return_exceptions=True)
        restore.cancel()
        watch.cancel()
        if tune_task is not None:
            tune_task.cancel()
        if bat_task is not None:
            bat_task.cancel()
        if ob_task is not None:
            ob_task.cancel()
            await asyncio.to_thread(onboard.save)
        if feed is not None:
            feed.cancel()
            await asyncio.to_thread(pad.close)
        if servo is not None and servo.state != "closed":
            # Do not relax servos on shutdown: close serial port only and keep session marker
            n = servo.holding()
            await asyncio.to_thread(servo.close)
            log.info(f"Studio exiting with open servo session ({n} holding); will resume on restart", cat="Bus", dedup=False)
        if net is not None:
            await net.stop()
        await robotd.stop()
        auth.flush()
        log.info("Studio stopped", cat="System", dedup=False)
        log.flush()

    # ---- Mobile HTTPS proxies (camera signaling & ASR) ----
    async def media_ws_proxy(ws: WebSocket):
        if phone_bridge is None:
            await ws.close(code=1011)
            return
        if not origin_ok(ws):
            await ws.close(code=4403)
            return
        _, sess = session_of(ws)
        if not sess:
            await ws.accept()
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            port = getattr(sensors, "camera_signaling_port", None)
            upstream = phone_bridge.MEDIA_UPSTREAM if port is None else f"ws://127.0.0.1:{port}"
            await phone_bridge.pump_media(ws, upstream=upstream)
        except Exception as exc:
            log.info(f"Camera signaling proxy closed ({_client_desc(ws)}): {exc}", cat="Connection")

    # Mobile ASR endpoint: transcribe uploaded PCM via on-device Vosk model.
    async def api_asr(request):
        no_store = {"Cache-Control": "no-store"}
        _, sess = session_of(request)
        if not sess:
            return JSONResponse({"ok": False, "error": "Session expired; log in again"}, 401, headers=no_store)
        if not origin_ok(request):
            return JSONResponse({"ok": False, "error": "Request verification failed; refresh and retry"}, 403, headers=no_store)
        if phone_bridge is None:
            return JSONResponse({"ok": False, "error": "Speech recognition module is not installed"}, 503, headers=no_store)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 12 * 1024 * 1024:
                return JSONResponse({"ok": False, "error": "Audio recording too long"}, 413, headers=no_store)
            body.extend(chunk)
        if len(body) < 3200:
            return JSONResponse({"ok": False, "error": "Audio too short; speak again"}, 400, headers=no_store)
        # free=1 uses open-vocabulary decoding for diagnostics.
        free = (request.query_params.get("free") or "").lower() in ("1", "true", "yes")
        detail = await asyncio.get_running_loop().run_in_executor(
            None, phone_bridge.transcribe_detail, body, not free)
        if detail.get("error"):
            return JSONResponse({"ok": False, "error": f"ASR failed: {detail['error']}"},
                                503, headers=no_store)
        return JSONResponse({"ok": True, "data": {"text": detail.get("text") or "",
                                                    "info": detail.get("info") or {}}},
                            headers=no_store)

    # Local CA certificate download for mobile HTTPS mic access (available before login).
    async def api_ca(request):
        no_store = {"Cache-Control": "no-store"}
        if not ca_cert:
            return Response("Local CA certificate not found", status_code=404, headers=no_store)
        try:
            body = await asyncio.to_thread(Path(ca_cert).read_bytes)
        except OSError:
            return Response("No local CA on this board", status_code=404, headers=no_store)
        return Response(body, media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="Microduck-Local-CA.crt"',
                                 **no_store})

    def maintenance_blocker():
        # Block maintenance power/restore during updates or IMU calibration
        if updates.installing() or updates.native_preparing():
            return "Software update in progress; wait for completion"
        job = getattr(imu, "job", None) if imu is not None else None
        if ((job and not (job.get("done") or job.get("failed")))
                or (imu is not None and getattr(imu, "blocks_updates", lambda: False)())):
            return "IMU calibration in progress; finish or cancel first"
        return None

    maintenance_handler = make_handler(auth, session_of, origin_ok, maintenance_blocker)
    routes = [
        Route("/api/maintenance", maintenance_handler, methods=["GET", "POST"]),
        Route("/healthz", healthz),
        Route("/api/session", api_session),
        Route("/api/ai", api_ai, methods=["GET", "POST"]),
        Route("/api/microphone", api_microphone, methods=["GET", "POST"]),
        Route('/api/updates',api_updates,methods=['GET','POST']),
        Route('/api/updates/upload',api_updates,methods=['POST']),
        Route("/api/login", api_login, methods=["POST"]),
        Route("/api/logout", api_logout, methods=["POST"]),
        Route("/api/password", api_password, methods=["POST"]),
        Route("/api/ssh", api_ssh, methods=["POST"]),
        Route("/api/logs", api_logs),
        Route("/api/diag", api_diag),
        Route("/api/profile", api_profile),
        Route("/api/camera", api_camera),
        Route("/api/tof", api_tof),
        Route("/api/models", api_models),
        Route("/api/models/op", api_model_op, methods=["POST"]),
        Route("/api/models/import", api_model_import, methods=["POST"]),
        Route("/model/meshes.bin", meshes),
        Route("/m", lambda request: RedirectResponse("/m.html", status_code=307)),
        # Mobile HTTPS helpers
        Route("/ca.crt", api_ca),
        Route("/api/asr", api_asr, methods=["POST"]),
        WebSocketRoute("/media-ws", media_ws_proxy),
        WebSocketRoute("/ws", ws_endpoint),
        Mount("/", AppStatic(directory=str(web_dir), html=True)),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    return HostGuard(app, guard, log)


def _run(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"(unavailable: {type(e).__name__}: {e})"


def _diag_zip(log, snap, raw):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", "Armature x Microduck Studio Diagnostics Bundle\n\n"
                   "Contents: last 2 days of Studio logs, current state, raw robotd payloads, versions, and robotd/padd journals.\n"
                   "Note: contains duck serial number and LAN IP. Remove system.txt and robotd-raw.json before sharing publicly.\n")
        for p in log.files(days=2):
            z.write(p, f"logs/{p.name}")
        z.writestr("state.json", json.dumps(snap, ensure_ascii=False, indent=1, default=str))
        z.writestr("robotd-raw.json", json.dumps(raw, ensure_ascii=False, indent=1, default=str))
        info = {"studio": VERSION, "python": sys.version, "platform": platform.platform(),
                "log": log.snapshot(), "time": time.strftime("%F %T %z")}
        z.writestr("versions.json", json.dumps(info, ensure_ascii=False, indent=1))
        deps = Path(__file__).resolve().parents[1] / "deps" / "VERSIONS.txt"
        if deps.is_file():
            z.write(deps, "deps-VERSIONS.txt")
        if sys.platform.startswith("linux"):
            z.writestr("journal-robotd.txt", _run(["journalctl", "-u", "robotd", "-n", "300", "--no-pager", "-o", "short-iso"]))
            z.writestr("journal-padd.txt", _run(["journalctl", "-u", "padd", "-n", "100", "--no-pager", "-o", "short-iso"]))
            z.writestr("system.txt", "\n".join(_run(c) for c in (["uptime"], ["free", "-m"], ["df", "-h", "/"], ["ip", "-brief", "addr"])))
    return buf.getvalue()


def precompress(web_dir):
    """Pre-compress meshes.bin to .gz when missing or outdated."""
    raw = Path(web_dir) / "model" / "meshes.bin"
    gz = raw.with_suffix(".bin.gz")
    try:
        if raw.is_file() and (not gz.is_file() or gz.stat().st_mtime < raw.stat().st_mtime):
            with open(raw, "rb") as f, gzip.open(gz, "wb", compresslevel=9) as g:
                g.write(f.read())
    except OSError:
        # Web dir is read-only: serve uncompressed file
        return None
    return gz
