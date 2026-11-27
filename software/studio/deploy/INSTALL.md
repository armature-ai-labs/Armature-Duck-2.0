# Studio Root Helpers & Deployment Guide

This directory contains the privileged helpers and systemd integration required by Armature × Microduck Studio:

- `microduck-helper`: Root helper invoked via `sudo -n /usr/local/sbin/microduck-helper <verb>` with JSON on stdin. Supports `bus-take`, `bus-give-back`, `bus-abort`, `pad-pair`, `pad-forget`, `wifi-hotspot-down`, `wifi-hotspot-up`, `calib-read`, `calib-commit`, `imu-recalibrate`, `ssh-status`, `ssh-password`, `sensors-apply`, `skill-table`, `phone-tls`, and `robotd-update` / `controller-update`.
- `microduck-maintenance`: Backup export/import and safe shutdown helper.
- `zzzz-studio-bus.conf`: `robotd.service.d` drop-in using `ConditionPathExists=!/run/microduck-helper/bus-held` to keep `robotd` stopped while Studio owns `/dev/ttyS2`.
- `microduck-studio.service`: Systemd unit for Studio on port `8000`.

## Installation

```bash
sudo sh install.sh radxa
```

## Uninstallation

```bash
sudo sh uninstall.sh [--purge]
```

Use `--purge` to also remove `/var/lib/microduck-helper`. Uninstallation refuses if the servo bus is currently held or an `imu-recalibrate` session is active.
