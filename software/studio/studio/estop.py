"""E-Stop state machine derived strictly from verified busStatus feedback."""
import time

STARTING_PHASES = ("configuring_bus", "waiting_for_bus", "configuration_error")
COMMISSIONING_UP = ("ready", "moving", "holding", "read_error", "stop_unconfirmed", "power_pending")
STARTING_LONG_S = 30.0
CONFIRM_TIMEOUT_S = 8.0
RESEND_AT_S = 2.0
CONFIRMED_FRESH_S = 2.5
OUTCOME_SHOW_S = 6.0
JOIN_S = 0.3


def robotd_up(bus):
    """Return True when robotd is out of startup phases (motion control or commissioning live snapshot)."""
    if not isinstance(bus, dict):
        return False
    mode, phase = bus.get("mode"), bus.get("phase")
    return (mode == "motion" and phase == "control") or (mode == "commissioning" and phase in COMMISSIONING_UP)


def relaxed(bus):
    """Return True if busStatus confirms all torque is OFF, False if not, or None if insufficient data."""
    if not isinstance(bus, dict):
        return None
    mode = bus.get("mode")
    if mode == "motion":
        native = bus.get("native")
        if not isinstance(native, dict) or "torque_state_confirmed" not in native:
            return None
        hd = native.get("hd1910") or {}
        return (bus.get("torque_off_pending") is not True
                and native.get("torque_state_confirmed") is False
                and hd.get("restore_pending") is False
                and hd.get("temporary_gains_applied") is False)
    if mode == "commissioning":
        if "torque_state" not in bus:
            return None
        return bus.get("torque_state") == "off" and not bus.get("torque_off_pending")
    return None


def torque_off(bus):
    """Return True if servo torque itself is confirmed OFF (ignoring hd1910 flags)."""
    if not isinstance(bus, dict):
        return False
    if bus.get("mode") == "commissioning":
        return bus.get("torque_state") == "off"
    native = bus.get("native")
    return isinstance(native, dict) and native.get("torque_state_confirmed") is False


def evidence(bus):
    """Extract diagnostic fields explaining why E-Stop confirmation could not complete."""
    if not isinstance(bus, dict):
        return {"busStatus": None}
    native = bus.get("native") if isinstance(bus.get("native"), dict) else {}
    hd = native.get("hd1910") if isinstance(native.get("hd1910"), dict) else {}
    return {"mode": bus.get("mode"), "phase": bus.get("phase"), "torque_off_pending": bus.get("torque_off_pending"),
            "torque_state": bus.get("torque_state"), "torque_state_confirmed": native.get("torque_state_confirmed"),
            "restore_pending": hd.get("restore_pending"), "temporary_gains_applied": hd.get("temporary_gains_applied")}


class EStop:
    def __init__(self):
        self.pressed_at = None
        self.sent_at = None
        self.resent = False
        self.saw_off = False
        self.send_error = None
        self.press_count = 0
        self.last_by = None
        self.confirm_ms = None
        self.outcome = None
        self.outcome_at = None
        self.outcome_why = ""

    @property
    def active(self):
        return self.pressed_at is not None

    def press(self, by=None, now=None):
        """Start or restart an E-Stop attempt; latest press resets timers."""
        now = time.monotonic() if now is None else now
        self.sent_at = None
        self.resent = False
        self.saw_off = False
        self.send_error = None
        self.pressed_at = now
        self.press_count += 1
        self.last_by = by
        self.outcome = None

    def belongs(self, started):
        """Return True if a send started at `started` belongs to the active attempt."""
        return self.active and started >= self.pressed_at - JOIN_S

    def sent(self, started, now=None):
        now = time.monotonic() if now is None else now
        if self.belongs(started) and self.sent_at is None:
            self.sent_at = now

    def send_failed(self, started, why, now=None):
        """Record a failed dispatch; fails immediately only if no prior receipt arrived."""
        if not self.belongs(started):
            return
        if self.sent_at is None:
            self.finish("send_failed", why, now=now)
        else:
            self.send_error = why

    def on_bus(self, bus, asked_at, now=None):
        """Process a busStatus reply issued after both press and sent timestamps."""
        now = time.monotonic() if now is None else now
        if not self.active or self.sent_at is None or asked_at < max(self.sent_at, self.pressed_at):
            return False
        if torque_off(bus):
            self.saw_off = True
        if relaxed(bus) is True:
            self.confirm_ms = round((now - self.pressed_at) * 1000)
            self.finish("confirmed", now=now)
            return True
        return False

    def finish(self, outcome, why="", now=None):
        now = time.monotonic() if now is None else now
        self.outcome, self.outcome_at, self.outcome_why = outcome, now, why
        self.pressed_at = None

    def elapsed(self, now=None):
        if not self.active:
            return None
        return (time.monotonic() if now is None else now) - self.pressed_at

    def status(self, *, present, bus, bus_age_s, now=None):
        """Return read-only UI status dict {"state": ..., "why": ...}."""
        now = time.monotonic() if now is None else now
        mode = bus.get("mode") if isinstance(bus, dict) else None
        phase = bus.get("phase") if isinstance(bus, dict) else None
        no_pad = "cut power directly" if (not present or phase in STARTING_PHASES or mode == "commissioning") \
            else "use gamepad Minus or cut power"
        if self.active:
            return {"state": "pending", "why": "Relaxing…"}
        if not present:
            return {"state": "unavailable", "why": f"Controller offline; {no_pad}"}
        fresh = bus is not None and bus_age_s is not None and bus_age_s <= CONFIRMED_FRESH_S
        if fresh and phase in STARTING_PHASES:
            return {"state": "unavailable", "why": f"Controller starting; {no_pad}"}
        if fresh and relaxed(bus) is True:
            return {"state": "confirmed", "why": "All 15 servos relaxed"}
        recent = self.outcome_at is not None and now - self.outcome_at < OUTCOME_SHOW_S
        if recent and self.outcome in ("timeout", "send_failed", "refused"):
            return {"state": "unavailable", "why": f"{self.outcome_why}; {no_pad}"}
        if not fresh:
            return {"state": "ready", "why": "Robot state stale; E-Stop will still send"}
        return {"state": "ready", "why": ""}
