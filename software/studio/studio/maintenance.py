import asyncio
import base64
import json
import re
import secrets
from pathlib import Path
import subprocess
import time
from starlette.responses import JSONResponse, Response

HELPER = "/usr/local/sbin/microduck-helper"
LIMIT = 24 * 1024 * 1024
RESULTS = Path("/var/lib/microduck-maintenance-results")


def receipt_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid restore receipt ID")
    return value


def run(op, data=None):
    r = subprocess.run(
        ["/usr/bin/sudo", "-n", HELPER, "maintenance-" + op],
        input=data,
        capture_output=True,
        timeout=45,
    )
    if r.returncode:
        if r.returncode < 0:
            raise ConnectionError("Maintenance request interrupted; check saved device results")
        try:
            failure = json.loads(r.stdout.splitlines()[-1])
            msg = failure.get("error") or failure.get("msg")
        except (IndexError, ValueError, AttributeError):
            msg = None
        if not msg:
            raise ConnectionError("Maintenance service unavailable or disconnected; check saved device results")
        raise ValueError(msg)
    return r.stdout


def make_handler(auth, session_of, origin_ok, blocker=None):
    lock = asyncio.Lock()

    async def handler(request):
        headers = {"Cache-Control": "no-store"}

        def reply(d, status=200):
            return JSONResponse(d, status_code=status, headers=headers)

        _, sess = session_of(request)
        if not auth.has_password() or not sess:
            return reply({"ok": False, "error": "Set a Studio password and sign in first"}, 401)
        if request.method == "POST" and (
            not origin_ok(request) or request.headers.get("x-csrf") != sess["csrf"]
        ):
            return reply({"ok": False, "error": "Request verification failed; refresh and retry"}, 403)
        try:
            if request.method == "GET" and "restore" in request.query_params:
                key = receipt_id(request.query_params["restore"])
                try:
                    return reply(json.loads((RESULTS / (key + ".json")).read_bytes()))
                except FileNotFoundError:
                    return reply({"id": key, "phase": "unknown"})
            if lock.locked():
                return reply({"ok": False, "error": "Another maintenance operation is in progress"}, 409)
            async with lock:
                if request.method == "GET":
                    data = await asyncio.to_thread(run, "export")
                    return Response(
                        data,
                        media_type="application/zip",
                        headers={
                            **headers,
                            "Content-Disposition": 'attachment; filename="microduck-backup-'
                            + time.strftime("%Y%m%d-%H%M%S")
                            + '.zip"',
                        },
                    )
                raw = bytearray()
                async for part in request.stream():
                    if len(raw) + len(part) > LIMIT:
                        return reply({"ok": False, "error": "Backup upload exceeds 24 MB limit"}, 413)
                    raw.extend(part)
                body = json.loads(raw)
                op = body.get("op")
                if op not in ["preview", "restore", "shutdown"]:
                    raise ValueError("Unsupported operation")
                if op in ["restore", "shutdown"] and body.get("confirmed") is not True:
                    raise ValueError("Confirmation required")
                if op in ["restore", "shutdown"] and blocker:
                    reason = blocker()
                    if reason:
                        raise ValueError(reason)
                if op == "restore" and not auth.check_password(
                    body.get("password") if isinstance(body.get("password"), str) else ""
                ):
                    raise ValueError("Re-enter Studio password before restoring; password incorrect")
                data = None
                if op in ["preview", "restore"]:
                    text = body.get("archive")
                    if not isinstance(text, str):
                        raise ValueError("Select a backup archive")
                    try:
                        data = base64.b64decode(text, validate=True)
                    except Exception:
                        raise ValueError("Invalid backup archive encoding")
                if op == "restore":
                    key = receipt_id(body.get("request_id", secrets.token_hex(16)))
                    data = key.encode("ascii") + b"\n" + data
                return reply(json.loads(await asyncio.to_thread(run, op, data)))
        except (ValueError, KeyError, TypeError) as exc:
            return reply({"ok": False, "error": str(exc)[:300]}, 400)
        except Exception:
            return reply({"ok": False, "error": "Maintenance operation failed; check device status"}, 503)

    return handler
