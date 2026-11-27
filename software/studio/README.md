# Armature × Microduck Studio

Web-based engineering console for the Microduck bipedal robot.

## Quick Start

```bash
# On the robot (Radxa ZERO 3W):
python3 -I run.py

# Local development against fake robotd/configd:
python3 -I dev/fake_robotd.py
python3 -I run.py --robotd tcp:127.0.0.1:17001 --configd tcp:127.0.0.1:17002
```

## Testing

```bash
python3 -I run_tests.py
```

## Architecture

- `studio/`: FastAPI + asyncio backend, Feetech servo bus session, IMU & joint calibration, gamepad pairing/drive, OTA update & maintenance helpers.
- `web/`: Desktop and mobile (`/m`) frontend built with local vendored Preact/HTM and Three.js (no external CDN requests).
- `deploy/`: Systemd unit, sudoers, `microduck-helper`, `microduck-maintenance`, TLS setup, and install/uninstall scripts.
- `dev/`: `fake_robotd.py`, `fake_tune.py`, `duckfs.py`, and `record_robotd.py` for offline development and testing.
