"""Pure-function action and combo definitions, validation, storage, and timeline expansion."""
import json
import math
import os
import re
import tempfile

TICK_S = 0.04
NOD_AMP, SHAKE_AMP, TILT_AMP = 0.5, 0.6, 0.4
NOD_PERIOD_S, SHAKE_PERIOD_S = 0.7, 0.8
CHATTER_PERIOD_S = 0.32
MAX_STEPS = 12
MAX_TOTAL_S = 20.0
MAX_USER_COMBOS = 20
NAME_MAX = 16
COMBO_FILE = "pad-combos.json"

SOUNDS = {
    "chirp": "Chirp",
    "greet": "Greet",
    "inquire": "Inquire",
    "coo": "Coo",
    "alarm": "Alarm",
    "peck": "Farewell",
}

SKILL_CN = {
    "ground_pick": "Ground Pick",
    "roulade": "Forward Roll",
    "kick_left": "Left Kick",
    "kick_right": "Right Kick",
    "roller_stand": "Roller Stand",
    "sit_toggle": "Sit / Stand",
}
SKILL_HOLD_S = {"kick_left": 0.8, "kick_right": 0.8, "roulade": 1.3, "ground_pick": 3.0}
SKILL_HOLD_DEFAULT_S = 2.0
NO_COMBO_SKILLS = {"sit_toggle"}
SKILL_RE = re.compile(r"^[a-z0-9_]{1,32}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")

STEP_CN = {"nod": "Nod", "shake": "Shake Head", "tilt": "Tilt Head", "mouth": "Open Mouth", "chatter": "Chatter",
           "sound": "Sound", "skill": "Skill", "wait": "Wait"}
NEEDS_POLICY = {"nod", "shake", "tilt", "skill"}


class Refused(Exception):
    """User-facing validation or state refusal for pad and combo operations."""


# ---------------- Built-in ----------------

ACTIONS = [
    {"id": "nod", "name": "Nod", "steps": [{"do": "nod", "times": 2}]},
    {"id": "shake", "name": "Shake Head", "steps": [{"do": "shake", "times": 2}]},
    {"id": "tilt", "name": "Tilt Head", "steps": [{"do": "tilt", "side": "right", "ms": 1200}]},
    {"id": "mouth", "name": "Open Mouth", "steps": [{"do": "mouth", "open": 0.9, "ms": 900}]},
]
BUILTIN_COMBOS = [
    {"id": "hello", "name": "Say Hello", "steps": [{"do": "sound", "tag": "greet"}, {"do": "nod", "times": 2},
                                                    {"do": "mouth", "open": 0.8, "ms": 500}]},
    {"id": "happy", "name": "Happy", "steps": [{"do": "sound", "tag": "chirp"}, {"do": "chatter", "times": 3},
                                                {"do": "nod", "times": 1}]},
    {"id": "ask", "name": "Curious", "steps": [{"do": "sound", "tag": "inquire"}, {"do": "tilt", "side": "right", "ms": 1000}]},
    {"id": "no", "name": "No", "steps": [{"do": "shake", "times": 3}, {"do": "sound", "tag": "coo"}]},
    {"id": "kicks", "name": "Double Kick", "steps": [{"do": "skill", "name": "kick_left"}, {"do": "wait", "ms": 600},
                                                      {"do": "skill", "name": "kick_right"}]},
]


# ---------------- Validation ----------------

def _num(v, lo, hi, default, what):
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise Refused(f"{what} must be a number")
    return max(lo, min(hi, v))


def clean_step(raw):
    """Validate and clamp a single combo step."""
    if not isinstance(raw, dict):
        raise Refused("Each step must be a {\"do\": ...} object")
    do = raw.get("do")
    if do == "nod" or do == "shake":
        return {"do": do, "times": int(_num(raw.get("times"), 1, 4, 2, "count"))}
    if do == "tilt":
        side = raw.get("side", "right")
        if side not in ("left", "right"):
            raise Refused("Tilt side must be left or right")
        return {"do": "tilt", "side": side, "ms": int(_num(raw.get("ms"), 300, 2000, 1000, "tilt duration"))}
    if do == "mouth":
        return {"do": "mouth", "open": round(_num(raw.get("open"), 0.1, 1.0, 0.8, "mouth open amount"), 2),
                "ms": int(_num(raw.get("ms"), 100, 2000, 600, "mouth duration"))}
    if do == "chatter":
        return {"do": "chatter", "times": int(_num(raw.get("times"), 1, 6, 3, "count"))}
    if do == "sound":
        tag = raw.get("tag")
        if tag not in SOUNDS:
            raise Refused(f"Unknown sound '{tag}' (allowed: {', '.join(SOUNDS)})")
        return {"do": "sound", "tag": tag}
    if do == "skill":
        name = raw.get("name")
        if not isinstance(name, str) or not SKILL_RE.match(name):
            raise Refused("Skill name must contain lowercase letters, digits, or underscores")
        if name in NO_COMBO_SKILLS:
            raise Refused("Sit/Stand changes posture and cannot be added to combos; use the Sit button")
        return {"do": "skill", "name": name}
    if do == "wait":
        return {"do": "wait", "ms": int(_num(raw.get("ms"), 0, 3000, 300, "wait duration"))}
    raise Refused(f"Unknown step '{do}' (allowed: {', '.join(STEP_CN)})")


def clean_combo(raw, builtin_ids=()):
    """Validate and normalize a user combo before saving."""
    if not isinstance(raw, dict):
        raise Refused("Combo must be an object")
    cid = raw.get("id")
    if not isinstance(cid, str) or not ID_RE.match(cid):
        raise Refused("ID must be lowercase letters, digits, - or _ (max 24 chars)")
    if cid in builtin_ids:
        raise Refused(f"'{cid}' is a built-in ID; choose another")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise Refused("Name is required")
    name = name.strip()[:NAME_MAX]
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise Refused("At least one step is required")
    if len(steps) > MAX_STEPS:
        raise Refused(f"Maximum of {MAX_STEPS} steps allowed")
    clean = [clean_step(s) for s in steps]
    total = duration(clean)
    if total > MAX_TOTAL_S:
        raise Refused(f"Combo too long (~{total:.0f}s); maximum is {MAX_TOTAL_S:.0f}s")
    return {"id": cid, "name": name, "steps": clean}


# ---------------- Timeline ----------------

def step_seconds(step):
    do = step["do"]
    if do == "nod":
        return step["times"] * NOD_PERIOD_S
    if do == "shake":
        return step["times"] * SHAKE_PERIOD_S
    if do in ("tilt", "mouth", "wait"):
        return step["ms"] / 1000
    if do == "chatter":
        return step["times"] * CHATTER_PERIOD_S
    if do == "sound":
        return 0.25
    if do == "skill":
        return SKILL_HOLD_S.get(step["name"], SKILL_HOLD_DEFAULT_S)
    return 0.0


def duration(steps):
    return sum(step_seconds(s) for s in steps)


def head_at(step, t):
    """Return (neck_pitch, head_pitch, head_yaw, head_roll) at time t, or None."""
    do = step["do"]
    if do == "nod":
        return (0.0, NOD_AMP * math.sin(2 * math.pi * t / NOD_PERIOD_S), 0.0, 0.0)
    if do == "shake":
        return (0.0, 0.0, SHAKE_AMP * math.sin(2 * math.pi * t / SHAKE_PERIOD_S), 0.0)
    if do == "tilt":
        total = step["ms"] / 1000
        ramp = min(0.25, total / 3)
        k = min(1.0, t / ramp) if t < total - ramp else max(0.0, (total - t) / ramp)
        return (0.0, 0.0, 0.0, (TILT_AMP if step["side"] == "right" else -TILT_AMP) * k)
    return None


def mouth_at(step, t):
    """Return mouth openness (0..1) at time t, or None."""
    do = step["do"]
    if do == "mouth":
        return step["open"] if t < step["ms"] / 1000 - TICK_S else 0.0
    if do == "chatter":
        return 0.85 if (t % CHATTER_PERIOD_S) < CHATTER_PERIOD_S / 2 else 0.05
    return None


def needs_policy(steps):
    return any(s["do"] in NEEDS_POLICY for s in steps)


def describe(steps):
    """Human-readable summary of combo steps."""
    out = []
    for s in steps:
        do = s["do"]
        if do in ("nod", "shake", "chatter"):
            out.append(f"{STEP_CN[do]} ×{s['times']}")
        elif do == "tilt":
            out.append("Tilt " + ("Right" if s["side"] == "right" else "Left"))
        elif do == "mouth":
            out.append("Open Mouth")
        elif do == "sound":
            out.append(f"Sound ({SOUNDS.get(s['tag'], s['tag'])})")
        elif do == "skill":
            out.append(SKILL_CN.get(s["name"], s["name"]))
        elif do == "wait":
            out.append(f"Wait {s['ms'] / 1000:g}s")
    return " → ".join(out)


# ---------------- Persistence (state_dir/pad-combos.json) ----------------

def load_user_combos(state_dir, log=None):
    """Load user-defined combo actions. Corrupt files or invalid entries are skipped and logged."""
    if not state_dir:
        return []
    path = os.path.join(state_dir, COMBO_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        if log:
            log.warn("Failed to load custom combo actions; ignoring file", cat="Gamepad", step=path, raw=f"{type(e).__name__}: {e}",
                     hint="Delete the corrupt file and recreate your combos")
        return []
    builtin = {c["id"] for c in BUILTIN_COMBOS + ACTIONS}
    out = []
    for raw in (data.get("combos") if isinstance(data, dict) else None) or []:
        try:
            out.append(clean_combo(raw, builtin))
        except Refused as e:
            if log:
                log.warn("Skipped invalid custom combo action", cat="Gamepad", step=path, raw=f"{raw!r:.200}: {e}")
    return out[:MAX_USER_COMBOS]


def save_user_combos(state_dir, combos):
    """Write atomically (write temp file then rename so power loss mid-write cannot corrupt the old file)."""
    if not state_dir:
        raise Refused("This duck has no state directory configured; cannot save")
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, COMBO_FILE)
    fd, tmp = tempfile.mkstemp(prefix=".pad-combos-", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "combos": combos}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise Refused(f"Failed to save: {type(e).__name__}: {e}") from None
    return path
