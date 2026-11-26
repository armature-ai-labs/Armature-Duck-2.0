"""Auto-configure Camera and ToF on boot when attached."""
import asyncio
import os
import re
import tomllib
from pathlib import Path

from . import helper

OFF_NAME = "sensors-auto-off"
SETTLE_S = 8.0           # Allow kernel driver probe time during early boot
TOF_UNITS = ("etc/systemd/system/tofd.service", "usr/lib/systemd/system/tofd.service", "lib/systemd/system/tofd.service")


def camera_present(root="/"):
    try:
        return any(re.fullmatch(r"\d+-0010", entry.name) for entry in (Path(root) / "sys/bus/i2c/drivers/imx219").iterdir())
    except OSError:
        return False


def media_source(root="/"):
    try:
        media = tomllib.loads((Path(root) / "etc/robot/robotd.toml").read_text(encoding="utf-8")).get("media")
    except (OSError, UnicodeError, ValueError):
        return None
    return media.get("source") if isinstance(media, dict) else None


def tof_pending(root="/"):
    """True when tofd is installed but not yet enabled or its socket is not up."""
    root = Path(root)
    return (any((root / unit).is_file() for unit in TOF_UNITS)
            and (not os.path.lexists(root / "etc/systemd/system/multi-user.target.wants/tofd.service")
                 or not (root / "run/tofd/tof.sock").exists()))


class SensorAuto:
    def __init__(self, log, state_dir=None, root="/", apply=None, available=None, busy=None, settle=SETTLE_S):
        self.log, self.root, self.settle = log, root, settle
        self.off = Path(state_dir) / OFF_NAME if state_dir else None
        self.apply = apply or helper.sensors_apply
        self.available = available or helper.available
        self.busy = busy or (lambda: False)
        self.last = None

    def view(self):
        return self.last

    def _done(self, state, note, **extra):
        self.last = {"state": state, "note": note, **extra}
        return self.last

    async def _camera_seen(self, timeout):
        """Return immediately if detected, otherwise poll until timeout for late driver probe."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            if camera_present(self.root):
                return True
            remaining = end - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.5, remaining))

    async def run_on_boot(self, by, late_settle=60.0, retry_delay=2.0):
        # A normal OTA restarts Studio before clearing the installation marker.
        # Re-read the configuration and detector state after every deferral.
        delay = retry_delay
        while True:
            result = await self.run(by)
            if result["state"] == "busy":
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2)
                continue
            if (late_settle > 0 and result["state"] == "ok" and result.get("camera_present") is False
                    and result.get("source") == "test" and await self._camera_seen(late_settle)):
                late_settle = 0
                continue
            return result

    async def run(self, by):
        if self.off is not None and self.off.exists():
            return self._done("off", None)
        source = media_source(self.root)
        if source not in ("camera", "test"):
            # Do not alter unknown camera config; still manage ToF.
            present, want = None, None
        else:
            present = await self._camera_seen(self.settle)
            want = "camera" if present and source == "test" else "test" if not present and source == "camera" else None
        tof = tof_pending(self.root)
        if want is None and not tof:
            return self._done("ok", None, camera_present=present, source=source)
        if not self.available():
            return self._done("unavailable", None, camera_present=present, source=source)
        if self.busy():
            return self._done("busy", "Sensors will auto-configure after software update finishes.", camera_present=present, source=source)
        ok, result, detail = await asyncio.to_thread(self.apply, by, want, tof)
        if not ok and result == "tof_failed" and want is not None:
            # The helper exits before touching the camera if tofd cannot start.
            self.log.warn("ToF service failed to start; continuing camera setup", cat="Sensors", raw=str(detail)[:400])
            ok, result, detail = await asyncio.to_thread(self.apply, by, want, False)
        detail = detail if isinstance(detail, dict) else {}
        if not ok and result in ("busy", "native_update_active"):
            return self._done("busy", "Waiting for helper operation to finish before auto-configuring sensors.", camera_present=present, source=source)
        if tof and detail.get("tof") == "enabled":
            self.log.info("Enabled ToF service at boot; check range readings on the ToF page", cat="Sensors", dedup=False)
        if ok and want == "camera":
            self.log.info("IMX219 detected; video source switched to camera", cat="Sensors", dedup=False)
            return self._done("ok", "Camera source selected and video service started.", camera_present=True, source="camera")
        if ok and want == "test":
            self.log.info("Camera not detected; switched video source to test pattern", cat="Sensors", dedup=False,
                          hint="If a camera is attached, power off, check ribbon cable orientation and seating, then reboot")
            return self._done("ok", "No camera detected at boot; using test pattern. Power off and check ribbon cable if attached.",
                              camera_present=False, source="test")
        if ok:
            return self._done("ok", None, camera_present=present, source=source)
        if result == "camera_unstable":
            self.log.warn("Camera detected, but video service failed with live source; reverted to test pattern", cat="Sensors", raw=str(detail)[:400],
                          hint="Likely a loose ribbon cable: power off, reseat cable, and reboot")
            return self._done("failed", "Camera detected, but live stream failed to start; reverted to test pattern. Power off and reseat ribbon cable.",
                              camera_present=True, source=source)
        self.log.warn("Auto camera / ToF setup did not complete", cat="Sensors", raw=f"{result}: {str(detail)[:400]}",
                      hint="Standing and walking are unaffected; Camera and ToF pages reflect existing config")
        return self._done("failed", None, camera_present=present, source=source, result=result)
