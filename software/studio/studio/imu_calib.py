"""IMU mount orientation calibration (standing/coarse): pure math using only stdlib `math`.

Collects gravity samples across still -> forward -> back -> left -> right tilts and solves
for mount quaternion, standing offset, per-step pass/fail, and quality metrics.

Conventions:
- Body frame B: +x forward (toward head), +y left, +z up. robot.state.safety.gravity is in B
  (~(0, 0, -1) when standing upright).
- Quaternions are always [w, x, y, z].
- Mount quaternion q_mount maps sensor -> body: v_body = q_mount * v_sensor * q_mount*.
"""
import math

DOWN = (0.0, 0.0, -1.0)
STEPS = ("still", "fwd", "back", "left", "right")
STEP_CN = {"still": "Stand Still", "fwd": "Tilt Forward", "back": "Tilt Back", "left": "Tilt Left", "right": "Tilt Right"}
TILTS = ("fwd", "back", "left", "right")
EXPECT_DIR = {"fwd": 0.0, "left": 90.0, "back": 180.0, "right": -90.0}   # Expected azimuth from +x CCW (deg)

# Criteria shared with web/imu.js RULES (frontend is slightly stricter so accepted data always passes here).
RULES = {
    "still_min_n": 15,              # Minimum samples for still step (~1.5s at 10 Hz)
    "still_max_spread_deg": 1.5,    # Max p90 spread during still step
    "level_deg": 5.0,               # Max angle from still gravity to count as level reference
    "ref_min_n": 5,                 # Min consecutive samples for level reference
    "ref_spread_deg": 1.5,          # Max spread within level reference
    "hold_band_deg": 1.8,           # Max angle from end sample to belong to hold segment
    "hold_min_n": 5,                # Min samples in hold segment
    "tilt_min_deg": 10.0,           # Min tilt angle from level reference
    "tilt_max_deg": 35.0,           # Max tilt angle (beyond this feet slip / readings degrade)
    "pair_max_deg": 60.0,           # Max deviation from 180 deg between opposite pair (fwd/back or left/right)
    "skew_max_deg": 20.0,           # Max skew of any single step from consensus forward
    "cross_max_deg": 12.0,          # Max non-orthogonality between fwd/back and left/right pairs
    "good_skew_deg": 10.0,          # Good grade threshold for skew
    "good_cross_deg": 6.0,          # Good grade threshold for cross
    "big_change_deg": 8.0,          # Confirm threshold when mount differs significantly from previous
    "stand_confirm_deg": 5.0,       # Confirm threshold when standing tilt exceeds robotd's 5 deg limit
    "flip_deg": 90.0,               # Confirm threshold when yaw flips >90 deg (likely swapped pairs)
}


# ---------------- Vectors ----------------

def _v(x):
    return (float(x[0]), float(x[1]), float(x[2]))


def add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a):
    return math.sqrt(dot(a, a))


def unit(a):
    """Unit vector; returns None for zero-length or non-finite vectors."""
    n = norm(a)
    if not (math.isfinite(n) and n > 1e-9):
        return None
    return (a[0] / n, a[1] / n, a[2] / n)


def angle_deg(a, b):
    """Angle between two vectors in degrees; None if either is zero."""
    ua, ub = unit(a), unit(b)
    if ua is None or ub is None:
        return None
    return math.degrees(math.acos(max(-1.0, min(1.0, dot(ua, ub)))))


def horiz(v, up):
    """Remove component of v along unit vector `up`."""
    return sub(v, scale(up, dot(v, up)))


def signed_angle_deg(a, b, axis):
    """Signed angle from `a` to `b` around `axis` (right-hand rule) projected onto the plane perpendicular to `axis`."""
    ha, hb = unit(horiz(a, axis)), unit(horiz(b, axis))
    if ha is None or hb is None:
        return None
    return math.degrees(math.atan2(dot(cross(ha, hb), axis), dot(ha, hb)))


def wrap180(d):
    return (d + 180.0) % 360.0 - 180.0


def circ_mean_deg(ds):
    s = sum(math.sin(math.radians(d)) for d in ds)
    c = sum(math.cos(math.radians(d)) for d in ds)
    if abs(s) < 1e-9 and abs(c) < 1e-9:
        return None
    return math.degrees(math.atan2(s, c))


# ---------------- Matrices (row-major 3x3 nested tuples) ----------------

def mat_mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def mat_vec(m, v):
    return tuple(m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3))


def mat_t(m):
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))


# ---------------- Quaternions [w, x, y, z] ----------------

def q_normalize(q):
    if len(q) != 4 or not all(math.isfinite(float(c)) for c in q):
        raise ValueError("Quaternion must have 4 finite numbers")
    n = math.sqrt(sum(float(c) * float(c) for c in q))
    if not n > 1e-12:
        raise ValueError("Quaternion has zero length")
    q = tuple(float(c) / n for c in q)
    return q if q[0] >= 0 else tuple(-c for c in q)     # Canonicalize w >= 0


def q_mul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw)


def q_conj(q):
    return (q[0], -q[1], -q[2], -q[3])


def q_rotate(q, v):
    return mat_vec(mat_from_q(q), v)


def mat_from_q(q):
    w, x, y, z = q_normalize(q)
    return ((1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)),
            (2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)),
            (2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)))


def q_from_mat(m):
    """Rotation matrix -> quaternion (Shepperd's method)."""
    tr = m[0][0] + m[1][1] + m[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        q = (0.25 * s, (m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s)
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2
        q = ((m[2][1] - m[1][2]) / s, 0.25 * s, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s)
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2
        q = ((m[0][2] - m[2][0]) / s, (m[0][1] + m[1][0]) / s, 0.25 * s, (m[1][2] + m[2][1]) / s)
    else:
        s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2
        q = ((m[1][0] - m[0][1]) / s, (m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, 0.25 * s)
    return q_normalize(q)


def q_angle_deg(q):
    """Rotation angle in degrees (0-180)."""
    w = abs(q_normalize(q)[0])
    return math.degrees(2 * math.acos(min(1.0, w)))


def q_between(a, b):
    """Shortest rotation from unit vector `a` to unit vector `b`."""
    a, b = unit(a), unit(b)
    c = dot(a, b)
    if c < -0.999999:
        # Opposite vectors: pick an arbitrary perpendicular axis and rotate 180 deg
        axis = unit(cross(a, (1.0, 0.0, 0.0))) or unit(cross(a, (0.0, 1.0, 0.0)))
        return (0.0, axis[0], axis[1], axis[2])
    x = cross(a, b)
    return q_normalize((1.0 + c, x[0], x[1], x[2]))


def gravity_from_quat(q):
    """Attitude (body -> world) -> gravity vector in body frame (matches robotd safety.gravity)."""
    r = mat_from_q(q)
    return (-r[2][0], -r[2][1], -r[2][2])


def compose_mount(correction, old_mount):
    """Compose new mount = correction * old_mount (sensor -> body)."""
    return q_normalize(q_mul(correction, old_mount))


# ---------------- Standard Mounts (24 axis-aligned orientations) ----------------

_AXIS_CN = {(1, 0): "forward", (1, 1): "left", (1, 2): "up", (-1, 0): "back", (-1, 1): "right", (-1, 2): "down"}


def _standard_mounts():
    out = []
    perms = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))
    for p in perms:
        for sx in (1, -1):
            for sy in (1, -1):
                for sz in (1, -1):
                    m = [[0.0] * 3 for _ in range(3)]
                    for col, (row, s) in enumerate(zip(p, (sx, sy, sz))):
                        m[row][col] = float(s)      # Sensor axis col -> body axis row
                    m = tuple(tuple(r) for r in m)
                    det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                           - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                           + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
                    if det > 0:
                        out.append(m)
    return out


STANDARD_MOUNTS = _standard_mounts()


def describe_mount(q):
    """Describe mount orientation in plain words using the nearest axis-aligned mount + angular offset."""
    best, best_deg = None, None
    for m in STANDARD_MOUNTS:
        d = q_angle_deg(q_mul(q_conj(q_from_mat(m)), q))
        if best_deg is None or d < best_deg:
            best, best_deg = m, d
    words = []
    for col, name in enumerate("xyz"):
        row = next(r for r in range(3) if best[r][col] != 0)
        words.append(f"chip {name} -> {_AXIS_CN[(int(best[row][col]), row)]}")
    return {"text": ", ".join(words), "off_deg": round(best_deg, 2), "quat": list(q_from_mat(best))}


# ---------------- Sampling ----------------

def _finite3(x):
    if not isinstance(x, (list, tuple)) or len(x) != 3:
        return None
    try:
        v = _v(x)
    except (TypeError, ValueError):
        return None
    return v if all(math.isfinite(c) for c in v) else None


def sample_gravity(s):
    """Extract unit gravity vector from sample dict/list; None if invalid or non-finite."""
    try:
        if isinstance(s, (list, tuple)):
            v = _finite3(s)
            return unit(v) if v else None
        if not isinstance(s, dict):
            return None
        if s.get("g") is not None:
            v = _finite3(s["g"])
            return unit(v) if v else None
        if s.get("acc") is not None:
            v = _finite3(s["acc"])
            a = unit(v) if v else None
            return None if a is None else scale(a, -1.0)
        if s.get("quat") is not None:
            return gravity_from_quat(s["quat"])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return None


def sum_vec(vs):
    s = (0.0, 0.0, 0.0)
    for v in vs:
        s = add(s, v)
    return s


def mean_dir(vs):
    return unit(sum_vec(vs)) if vs else None


def spread_deg(vs, m):
    return max((angle_deg(v, m) or 0.0 for v in vs), default=0.0)


def spread_p90_deg(vs, m):
    """90th percentile angular spread from `m` (ignores isolated outlier spikes in still step)."""
    ds = sorted(angle_deg(v, m) or 0.0 for v in vs)
    return ds[int(0.9 * (len(ds) - 1))] if ds else 0.0


def trimmed_mean_dir(vs):
    """Mean direction after dropping the furthest 10% outliers."""
    m = mean_dir(vs)
    if m is None:
        return None
    lim = spread_p90_deg(vs, m)
    keep = [v for v in vs if (angle_deg(v, m) or 0.0) <= lim + 1e-9]
    return mean_dir(keep) or m


def group_steps(samples):
    """Group a flat sample list by `step`, or normalize an existing step dict."""
    if isinstance(samples, dict):
        return {k: list(samples.get(k) or []) for k in STEPS}
    out = {k: [] for k in STEPS}
    for s in samples or []:
        if isinstance(s, dict) and s.get("step") in out:
            out[s["step"]].append(s)
    return out


def tilt_of(g0, g):
    """Tilt angle (deg) and azimuth (deg from +x) of `g` relative to `g0`."""
    t = angle_deg(g0, g)
    up = scale(g0, -1.0)
    d = signed_angle_deg((1.0, 0.0, 0.0), sub(g, g0), up) if unit(horiz(sub(g, g0), up)) else None
    return t, d


# ---------------- Hold Segment & Level Reference ----------------

def _run_back(gs, end, band):
    """Walk backward from gs[end] within `band` deg, skipping isolated single-sample spikes."""
    last = gs[end]
    idx, skipped, j = [end], 0, end - 1
    while j >= 0:
        if angle_deg(gs[j], last) <= band:
            idx.append(j)
            j -= 1
        elif j - 1 >= 0 and angle_deg(gs[j - 1], last) <= band:
            skipped += 1                  # Single spike: skip and continue
            j -= 1
        else:
            break
    return sorted(idx), skipped


def find_hold(gs, rules=RULES):
    """Find the longest hold segment ending at the last (or second-to-last) sample."""
    if not gs:
        return [], 0
    best = _run_back(gs, len(gs) - 1, rules["hold_band_deg"])
    if len(gs) >= 2:
        alt = _run_back(gs, len(gs) - 2, rules["hold_band_deg"])
        if len(alt[0]) > len(best[0]) + 1:
            best = alt
    return best


def find_ref(gs, g0, before, rules=RULES):
    """Find the last stable level window before `before` within level_deg of `g0`."""
    n = rules["ref_min_n"]
    for end in range(before, n - 1, -1):
        win = gs[end - n:end]
        if all((angle_deg(g0, v) or 0.0) <= rules["level_deg"] for v in win):
            m = mean_dir(win)
            if m is not None and spread_deg(win, m) <= rules["ref_spread_deg"]:
                return m
    return None


# ---------------- Per-Step Checks ----------------

def _r(x, nd=2):
    return None if x is None else round(x, nd)


def _gs(samples):
    return [g for g in (sample_gravity(s) for s in samples) if g is not None]


def check_still(samples, rules=RULES):
    gs = _gs(samples)
    out = {"step": "still", "name": STEP_CN["still"], "n": len(gs), "dropped": len(samples) - len(gs), "ok": False}
    if len(gs) < rules["still_min_n"]:
        out["msg"] = f"Only {len(gs)} still samples (need at least {rules['still_min_n']}); hold still longer"
        return out, None
    g0 = trimmed_mean_dir(gs)
    if g0 is None:
        out["msg"] = "Still samples sum to zero; cannot determine direction"
        return out, None
    spread = spread_p90_deg(gs, g0)
    out["spread_deg"] = _r(spread)
    out["max_dev_deg"] = _r(spread_deg(gs, g0))
    if spread > rules["still_max_spread_deg"]:
        out["msg"] = f"Too much motion while still ({spread:.1f} deg, max {rules['still_max_spread_deg']} deg); keep steady and retry"
        return out, None
    out.update(ok=True, msg="Steady")
    return out, g0


def check_tilt(step, samples, g0, rules=RULES):
    """Return (step_summary, mean_hold_gravity, level_ref)."""
    gs = _gs(samples)
    name = STEP_CN[step]
    out = {"step": step, "name": name, "n": len(gs), "dropped": len(samples) - len(gs), "ok": False}
    if not gs:
        out["msg"] = f"'{name}' has no samples"
        return out, None, None
    idx, skipped = find_hold(gs, rules)
    hold = [gs[i] for i in idx]
    ref = find_ref(gs, g0, idx[0], rules)
    out["ref"] = "level hold" if ref is not None else "table still"
    ref = ref or g0
    out["ref_offset_deg"] = _r(angle_deg(g0, ref))
    g = mean_dir(hold)
    tilt = angle_deg(ref, g) if g is not None else None
    tmax = max(angle_deg(ref, v) for v in hold)       # Max check uses peak hold sample, not mean
    out.update(n_used=len(hold), skipped=skipped, tilt_deg=_r(tilt), max_tilt_deg=_r(tmax),
               spread_deg=_r(spread_deg(hold, g) if g else None))
    if len(hold) < rules["hold_min_n"] or g is None:
        out["msg"] = f"'{name}' did not hold steady after tilting ({len(hold)} samples); hold 1s at angle"
        return out, None, None
    if tilt < rules["tilt_min_deg"]:
        out["msg"] = f"'{name}' tilted only {tilt:.1f} deg (need at least {rules['tilt_min_deg']:.0f} deg)"
        return out, None, None
    if tmax > rules["tilt_max_deg"]:
        out["msg"] = f"'{name}' tilted {tmax:.0f} deg (max {rules['tilt_max_deg']:.0f} deg); too steep"
        return out, None, None
    out.update(ok=True, msg=f"Tilted {tilt:.0f} deg")
    return out, g, ref


# ---------------- Solver ----------------

def solve(samples, frame="body", old_mount=None, home_gravity=DOWN, rules=None, mode="full", old_verified=True,
          old_source=None):
    """Solve IMU mount orientation from gravity samples across still and tilt steps."""
    rules = dict(RULES, **(rules or {}))
    if frame not in ("body", "sensor"):
        raise ValueError("frame must be 'body' or 'sensor'")
    if mode not in ("full", "coarse"):
        raise ValueError("mode must be 'full' or 'coarse'")
    hg = unit(_v(home_gravity))
    if hg is None:
        raise ValueError("home_gravity is zero")
    steps = group_steps(samples)
    res = {"ok": False, "reason": None, "redo": [], "steps": {}, "frame": frame, "mode": mode, "rules": rules,
           "notes": [], "confirm": []}
    need = TILTS if mode == "full" else ("fwd",)
    optional = () if mode == "full" else ("back",)

    st, g0 = check_still(steps["still"], rules)
    res["steps"]["still"] = st
    gs, refs = {}, {}
    for k in need + optional:
        if g0 is None:
            res["steps"][k] = {"step": k, "name": STEP_CN[k], "ok": False, "msg": "Still step must pass first"}
            continue
        if k in optional and not steps[k]:
            continue
        res["steps"][k], g, ref = check_tilt(k, steps[k], g0, rules)
        if g is not None:
            gs[k], refs[k] = g, ref
    bad = [res["steps"][k] for k in ("still",) + need if not res["steps"][k]["ok"]]
    bad += [res["steps"][k] for k in optional if k in res["steps"] and not res["steps"][k]["ok"]]
    if bad:
        res["reason"] = bad[0]["msg"]
        # If still failed, redo still only and keep tilt samples
        res["redo"] = ["still"] if g0 is None else [b["step"] for b in bad]
        return res

    up = scale(g0, -1.0)
    # Azimuth zero: project +x onto horizontal plane (fall back to +y if +x is vertical)
    axis0 = unit(horiz((1.0, 0.0, 0.0), up)) or unit(horiz((0.0, 1.0, 0.0), up))
    c = {}
    for k in gs:
        d = horiz(sub(gs[k], refs[k]), up)
        az = signed_angle_deg(axis0, d, up)
        if az is None:
            res["reason"] = f"Cannot compute tilt direction for '{STEP_CN[k]}'; redo this step"
            res["redo"] = [k]
            return res
        c[k] = wrap180(az - EXPECT_DIR[k])      # Estimated forward azimuth for this step
    q = {}
    res["quality"] = q

    def pair(a, b):
        if a in c and b in c:
            d = abs(wrap180(c[a] - c[b]))
            q[f"{'fb' if a == 'fwd' else 'lr'}_pair_deg"] = _r(d)
            if d > rules["pair_max_deg"]:
                res["reason"] = (f"{STEP_CN[a]} and {STEP_CN[b]} do not oppose each other (off by {d:.0f} deg); "
                                 "did you tilt the same direction twice?")
                res["redo"] = [a, b]
                return False
        return True

    if not pair("fwd", "back") or not pair("left", "right"):
        return res
    if mode == "full":
        psi_fb = circ_mean_deg([c["fwd"], c["back"]])
        psi_lr = circ_mean_deg([c["left"], c["right"]])
        crs = abs(wrap180(psi_fb - psi_lr))
        q["cross_deg"] = _r(crs)
        if crs > 90.0:
            res["reason"], res["redo"] = _swapped_reason(psi_fb, psi_lr, frame == "body" and old_verified)
            return res
    psi = circ_mean_deg(list(c.values()))
    skews = {k: wrap180(c[k] - psi) for k in c}
    for k, s in skews.items():
        res["steps"][k]["skew_deg"] = _r(abs(s))
    worst = max(skews, key=lambda k: abs(skews[k]))
    q["max_skew_deg"] = _r(abs(skews[worst]))
    if abs(skews[worst]) > rules["skew_max_deg"]:
        res["reason"] = (f"'{STEP_CN[worst]}' was skewed by {abs(skews[worst]):.0f} deg; "
                         "tilt straight along the 3D arrow without twisting")
        res["redo"] = [worst]
        return res
    if mode == "full":
        if crs > rules["cross_max_deg"]:
            res["reason"] = (f"Forward/back and left/right axes are not orthogonal (off by {crs:.0f} deg); "
                             "avoid twisting and tilt straight along the 3D arrows")
            if frame == "body" and old_verified:
                res["redo"] = ["left", "right"] if abs(wrap180(psi_lr)) > abs(wrap180(psi_fb)) else ["fwd", "back"]
            else:
                res["redo"] = list(TILTS)
            return res
        q["grade"] = "good" if crs <= rules["good_cross_deg"] and abs(skews[worst]) <= rules["good_skew_deg"] else "ok"
        q["yaw_uncert_deg"] = _r(max(crs / 2, 0.5))
    else:
        q["grade"] = "coarse"
        res["notes"].append("Coarse calibration: only Still and Tilt Forward were required; run full standing calibration once the duck can stand")
    p = math.radians(psi)
    x_dir = add(scale(axis0, math.cos(p)), scale(cross(up, axis0), math.sin(p)))
    return _finish(res, g0, up, x_dir, hg, frame, old_mount, old_verified, old_source, rules)


def _swapped_reason(psi_fb, psi_lr, trust_old):
    """Opposite pairs differ by ~180 deg (one pair was swapped)."""
    if trust_old:
        if abs(wrap180(psi_lr)) > abs(wrap180(psi_fb)):
            return "Left and Right were swapped: use the duck's own left/right (when facing the duck, its left is on your right)", ["left", "right"]
        return "Forward and Back were swapped: tilting the head downward is Forward", ["fwd", "back"]
    return ("One pair was swapped: Forward tilts the head downward; Left/Right are relative to the duck itself",
            list(TILTS))


def _finish(res, g0, up, x_dir, hg, frame, old_mount, old_verified, old_source, rules):
    """Build orthonormal basis from up + forward, compute mount/correction, and check confirmation prompts."""
    y = unit(cross(up, x_dir))
    x = cross(y, up)
    r_lm = (x, y, up)                        # Rows = horizontal frame L axes
    r_bl = mat_from_q(q_between(DOWN, hg))   # Body attitude relative to L at HOME
    r_bm = mat_mul(r_bl, r_lm)
    corr = q_from_mat(r_bm)
    res["correction"] = [round(c, 9) for c in corr]

    # Breakdown: standing tilt (pitch/roll) and heading (yaw)
    hp = math.degrees(math.atan2(hg[0], -hg[2]))
    hroll = math.degrees(math.atan2(hg[1], -hg[2]))
    parts = {"total_deg": _r(q_angle_deg(corr))}
    if frame == "body":
        parts["pitch_deg"] = _r(math.degrees(math.atan2(g0[0], -g0[2])) - hp)
        parts["roll_deg"] = _r(math.degrees(math.atan2(g0[1], -g0[2])) - hroll)
        parts["yaw_deg"] = _r(signed_angle_deg((1.0, 0.0, 0.0), x, up))
        parts["stand_tilt_deg"] = _r(angle_deg(g0, hg))
    res["parts"] = parts

    # New mount
    if frame == "sensor":
        mount = corr
    elif old_mount is not None:
        mount = compose_mount(corr, q_normalize(old_mount))
    else:
        mount = None
        res["notes"].append("No previous mount provided; computed correction only")
    if mount is not None:
        res["mount"] = [round(c, 9) for c in mount]
        res["standard"] = describe_mount(mount)

    # Notes & confirmations
    full = res["mode"] == "full"
    if frame == "body" and old_verified and parts.get("yaw_deg") is not None:
        tot = q_angle_deg(corr)
        if abs(parts["yaw_deg"]) > rules["flip_deg"]:
            if old_source == "coarse":
                res["confirm"].append(
                    f"Heading changed by {parts['yaw_deg']:+.0f} deg: previous coarse calibration may have been reversed. "
                    "Verify against the physical IMU module orientation before saving")
            else:
                res["confirm"].append(
                    f"Heading changed by {parts['yaw_deg']:+.0f} deg (likely both pairs reversed, i.e. duck turned around). "
                    "Only confirm if you physically rotated the IMU module")
        elif tot > rules["big_change_deg"]:
            res["confirm"].append(f"Orientation differs by {tot:.1f} deg from current mount; confirm module mount changed before saving")
    if frame == "body" and full:
        st = parts["stand_tilt_deg"]
        if st is not None and st > rules["stand_confirm_deg"]:
            res["confirm"].append(
                f"Standing reading is {st:.1f} deg from official HOME (robotd allows {rules['stand_confirm_deg']:.0f} deg): "
                "check leg zeroing, level surface, or hand contact during Still step before saving")
        res["notes"].append(
            f"Standing reading vs HOME: pitch {parts['pitch_deg']:+.1f} deg, roll {parts['roll_deg']:+.1f} deg (folded into mount; "
            "calibrate leg zeros first so leg skew is not absorbed into the IMU)")
    if not full and res.get("standard"):
        s = res["standard"]
        res["confirm"].append(
            f"Coarse result: {s['text']}" + (f" (offset {s['off_deg']:.0f} deg)" if s["off_deg"] >= 1 else "")
            + ". Confirm this matches the physical IMU mounting before saving")
    if res.get("standard") and res["standard"]["off_deg"] > 20:
        res["notes"].append(f"Result is {res['standard']['off_deg']:.0f} deg from nearest standard mount ({res['standard']['text']}); is the module tilted?")
    res["ok"] = True
    return res
