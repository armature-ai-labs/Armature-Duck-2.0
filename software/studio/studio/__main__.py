"""Start Studio. Launch via run.py (which adds deps/ to sys.path):

    On the duck:  python3 -I run.py --listen 0.0.0.0:8000
    On desktop:   python3 -I run.py --listen 127.0.0.1:8000 --robotd tcp:127.0.0.1:17001 --configd tcp:127.0.0.1:17002
                  (start fake robotd first: python3 -I dev/fake_robotd.py)
    Self-check:   python3 -I run.py --selfcheck
    Set password: Web UI 'More -> Password', or as radxa user run: python3 -I run.py set-password
"""
import argparse
import asyncio
import getpass
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def camera_port(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("Camera signaling port must be 1-65535")
    return port


def default_state_dir():
    if sys.platform.startswith("linux") and os.path.isdir("/var/lib/microduck-studio"):
        return "/var/lib/microduck-studio"
    return str(ROOT / "state")


def parse(argv=None):
    ap = argparse.ArgumentParser(prog="studio", description="Armature x Microduck Studio")
    ap.add_argument("cmd", nargs="?", default="serve", choices=["serve", "set-password"])
    ap.add_argument("--listen", default=None, help="Listen host:port (default 0.0.0.0:8000 on duck; 127.0.0.1:18080 in bench mode)")
    ap.add_argument("--tls-cert", default=None,
                    help="HTTPS certificate chain (PEM). Used with --tls-key for mobile mic. Defaults to /etc/microduck/tls/duck.crt")
    ap.add_argument("--tls-key", default=None,
                    help="HTTPS private key (PEM); must be paired with --tls-cert. Defaults to /etc/microduck/tls/duck.key")
    ap.add_argument("--robotd", default=None, help="Default unix:/run/robotd.sock; disabled in bench mode")
    ap.add_argument("--configd", default=None, help="Default unix:/run/configd.sock; disabled in bench mode")
    ap.add_argument("--tof", default="unix:/run/tofd/tof.sock", help="ToF read-only socket address; 'none' to disable")
    ap.add_argument("--camera-socket", default="unix:/run/mediad/media.sock", help="Camera service read-only probe address; 'none' to disable")
    ap.add_argument("--camera-signaling-port", type=camera_port, default=8443, help="Local mediad video signaling port (default 8443)")
    ap.add_argument("--bench", action="store_true",
                    help="Bench mode: tune servos via USB-TTL adapter without robotd; skips robotd and opens browser to Servo page")
    ap.add_argument("--state-dir", default=None, help="Directory for passwords and sessions (default ./state)")
    ap.add_argument("--log-dir", default=None, help="Log directory (default ./logs)")
    ap.add_argument("--web-dir", default=str(ROOT / "web"))
    ap.add_argument("--name", action="append", default=[], help="Additional accepted hostnames (repeatable)")
    ap.add_argument("--duck-label", default=None, help="Display name in UI; defaults to hostname (duck-xxxx on duck)")
    ap.add_argument("--net-state", default="/run/microduck/net.json",
                    help="Network state file written by netcheck (default /run/microduck/net.json; Wi-Fi UI hidden if missing)")
    ap.add_argument("--duck-color", default="#e0703a", help="Accent color")
    ap.add_argument("--servo-port", default="/dev/ttyS2" if sys.platform.startswith("linux") else None,
                    help="Servo bus serial port (/dev/ttyS2 on duck; e.g. COM5 or /dev/ttyUSB0 on desktop)")
    ap.add_argument("--servo-sim", action="store_true", help="Dev: use simulated 15-servo bus on the Servo page")
    ap.add_argument("--selfcheck", action="store_true", help="Read-only self-check: print version, probe sockets once, and exit")
    ap.add_argument("--dev-autologin", action="store_true",
                    help="Dev: skip login for localhost requests (only allowed with --listen 127.0.0.1)")
    ap.add_argument("--dev-duck-root", default=None,
                    help="Dev: read tuning/model tree from this directory (created by dev/duckfs.py) instead of /etc and /opt; requires --robotd tcp:...")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse(argv)
    # Resolve defaults upfront so Robotd and selfcheck can use them.
    if a.listen is None:
        a.listen = "127.0.0.1:18080" if a.bench else "0.0.0.0:8000"
    if a.robotd is None:
        a.robotd = "none" if a.bench else "unix:/run/robotd.sock"
    if a.configd is None:
        a.configd = "none" if a.bench else "unix:/run/configd.sock"
    state_dir = a.state_dir or default_state_dir()
    log_dir = a.log_dir or str(ROOT / "logs")

    from .auth import Auth
    from .log import setup

    if a.cmd == "set-password":
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise SystemExit("Do not run with sudo: run as the radxa user so Studio can read the password file")
        pw = getpass.getpass("New admin password (at least 6 characters): ")
        if pw != getpass.getpass("Confirm password: "):
            raise SystemExit("Passwords do not match; unchanged")
        Auth(state_dir).set_password(pw)
        print("Password updated; all previous sessions invalidated.")
        return

    log = setup(log_dir)
    import starlette
    import uvicorn

    from .app import VERSION, build_app, precompress
    from .robotd import Robotd

    log.info(f"Dependencies: starlette {starlette.__version__} · uvicorn {uvicorn.__version__} · from {Path(starlette.__file__).parent.parent}",
             cat="System", dedup=False)
    rd = Robotd(a.robotd, a.configd, log)

    if a.selfcheck:
        async def check():
            r = await rd.selfcheck()
            await rd.stop()
            return r
        print(f"Studio {VERSION} · Python {sys.version.split()[0]}")
        for k, v in asyncio.run(check()).items():
            print(f"  {k:12s} {v}")
        return

    auth = Auth(state_dir, log)
    if not auth.has_password():
        log.info("No password set: anyone on the same network can access the UI (set one in More -> Password)", cat="System",
                 hint=f"Set in the web UI, or as radxa user run: python3 -I {ROOT / 'run.py'} set-password (do not use sudo)")
    host, _, port = a.listen.rpartition(":")
    # Always report HTTP URL: DualServer accepts both HTTP and HTTPS on the same port; HTTPS is only required for mobile mic.
    scheme = "http"
    tls = None
    auto_tls = False
    # Local CA certificate generated on first boot goes here; used automatically if --tls-* is omitted.
    tls_dir = "/etc/microduck/tls"
    auto_cert, auto_key = os.path.join(tls_dir, "duck.crt"), os.path.join(tls_dir, "duck.key")
    if (not a.bench and not a.tls_cert and not a.tls_key
            and ROOT == Path("/opt/microduck/software/current/studio").resolve()
            and os.path.isfile("/usr/local/sbin/microduck-helper")):
        import subprocess
        try:
            subprocess.run(["/usr/bin/sudo", "-n", "/usr/local/sbin/microduck-helper", "phone-tls"],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=25, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warn("Phone HTTPS certificate setup failed", cat="System", raw=str(exc), dedup=False,
                     hint="Run sudo /usr/local/sbin/microduck-helper phone-tls to inspect the error")
    if not a.tls_cert and not a.tls_key and os.path.isfile(auto_cert) and os.path.isfile(auto_key):
        a.tls_cert, a.tls_key = auto_cert, auto_key
        auto_tls = True
    if bool(a.tls_cert) != bool(a.tls_key):
        raise SystemExit("--tls-cert and --tls-key must be provided together")
    if a.tls_cert and a.tls_key:
        for path in (a.tls_cert, a.tls_key):
            if not os.path.isfile(path):
                raise SystemExit(f"Certificate file not found: {path}")
        from .dual_listen import load_tls
        try:
            tls = load_tls(a.tls_cert, a.tls_key)
        except OSError as exc:      # Unreadable or cert/key mismatch (ssl.SSLError is an OSError)
            if not auto_tls:
                raise SystemExit(f"Invalid certificate: {exc}")
            # Broken auto-discovered cert must not prevent web UI from starting: fall back to HTTP (mobile mic disabled)
            log.warn("HTTPS certificate unusable; starting in HTTP-only mode", cat="System", raw=str(exc), dedup=False,
                     hint="Regenerate with: sudo /usr/local/sbin/microduck-helper phone-tls, then restart Studio")
    ca_cert = os.path.join(tls_dir, "ca.crt")
    if not os.path.isfile(ca_cert):
        ca_cert = None
    if a.dev_autologin and host != "127.0.0.1":
        raise SystemExit("--dev-autologin can only be used with --listen 127.0.0.1:PORT")
    if a.dev_autologin and (os.path.exists("/run/robotd.sock") or a.robotd.startswith("unix:")):
        # Local forwards on the duck (reverse proxy, SSH tunnel) also arrive from 127.0.0.1; enabling this would grant admin to everyone
        raise SystemExit("--dev-autologin is only for desktop development with fake robotd, never on the duck")
    # Do not hardcode name in image: use hostname if --duck-label is omitted (fresh SD card boots as duck-xxxx)
    label = a.duck_label or socket.gethostname()
    duck = {"label": label, "short": label.split(" ")[-1], "color": a.duck_color}
    names = list(a.name)
    precompress(a.web_dir)
    from .servo import ServoSession, load_directions
    # Servo page is always available: defaults to /dev/ttyS2 on the duck; on desktop without --servo-port, pick serial port in UI
    servo = ServoSession(log, port=a.servo_port, sim=a.servo_sim, directions=load_directions(state_dir),
                         state_dir=state_dir)
    from .imu import ImuBackend
    imu = ImuBackend(rd, log, state_dir, root=a.dev_duck_root or "/")
    from .pad import default_pad
    # Bench mode (--bench) does not connect to robotd, so no gamepad backend
    pad = None if a.bench else default_pad(log, configd_addr=a.configd, robotd_addr=a.robotd, state_dir=state_dir)
    from .onboard import Onboarding
    # Overview onboarding checklist (saved in state_dir/onboarding.json); bench mode skips robotd checks
    onboard = Onboarding(state_dir, log)
    from .commit import CommitService
    # Onboarding step 3 ("Write to robotd"): readiness, preview, and helper invocation (preview-only on desktop)
    commit = CommitService(rd, log, state_dir)
    from .battery_svc import BatteryService
    # Battery estimator (servo voltage lookup, warn-only): settings in state_dir/battery-settings.json
    battery = BatteryService(rd, log, state_dir, servo=servo, onboard=onboard)
    from .net import Net
    # Network status: on desktop dev / without helper, connecting to Wi-Fi does not tear down hotspot (logged)
    net = Net(a.configd, a.net_state, state_dir, log, dev_mode=bool(a.dev_autologin), port=int(port),
              scheme=scheme)
    # Overview tuning & settings models (read-only on bench mode where robotd is not connected)
    tune = None
    models = None
    if not a.bench:
        from .tune import Tune
        if a.dev_duck_root and not a.robotd.startswith("tcp:"):
            raise SystemExit("--dev-duck-root can only be used on desktop with fake robotd (--robotd tcp:...)")
        tune = Tune(rd, log, state_dir, root=a.dev_duck_root or "/", duck=duck)
        from .models import Models
        models = Models(rd, log, state_dir, tune=tune, root=a.dev_duck_root or "/")
    from .sensors import Sensors
    sensors = Sensors(log=log, root=a.dev_duck_root or "/", tof_addr=a.tof,
                      camera_signaling_port=a.camera_signaling_port, camera_addr=a.camera_socket)
    from .sensors_auto import SensorAuto
    # Auto-enable camera and ToF on boot when detected; disabled in bench mode
    sensor_auto = None if a.bench else SensorAuto(log, state_dir, root=a.dev_duck_root or "/")
    app = build_app(auth=auth, robotd=rd, log=log, web_dir=a.web_dir, duck=duck, names=names, state_dir=state_dir,
                    dev_autologin=a.dev_autologin, servo=servo, imu=imu, pad=pad, onboard=onboard, battery=battery,
                    tune=tune, net=net, commit=commit, models=models, sensors=sensors, sensor_auto=sensor_auto, ca_cert=ca_cert)
    any_host = host in ("0.0.0.0", "")
    log.info(f"Web URL: {scheme}://{'<duck-ip>' if any_host else host}:{port}/", cat="System", dedup=False)
    if tls:
        tls_host = socket.gethostname() if any_host else host
        log.info(f"HTTPS also listening on the same port (mobile mic available at https://{tls_host}:{port}/m)", cat="System", dedup=False)
    elif not a.tls_cert:
        log.info("No HTTPS certificate: running HTTP only (mobile mic disabled; enable with: sudo /usr/local/sbin/microduck-helper phone-tls)",
                 cat="System", dedup=False)
    if a.bench:
        import threading
        import webbrowser
        url = f"{scheme}://{host or '127.0.0.1'}:{port}/#/servos"
        log.info(f"Bench mode: not connecting to robotd; opening {url} — select a serial port on the Servo page", cat="System", dedup=False)
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    from .ws_protocol import MusicWebSocketProtocol
    serve = dict(host=host or "0.0.0.0", port=int(port), ws=MusicWebSocketProtocol, http="h11", lifespan="on",
                 log_level="warning", server_header=False, proxy_headers=False, access_log=False)
    if tls is None:
        uvicorn.run(app, **serve)
        return
    from .dual_listen import DualServer
    server = DualServer(uvicorn.Config(app, **serve), tls)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    if not server.started:
        sys.exit(3)                 # Match uvicorn.run behavior: non-zero exit so systemd restarts on failure


if __name__ == "__main__":
    main()
