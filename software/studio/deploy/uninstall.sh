#!/bin/sh
# Uninstall Studio root helper, robotd guard drop-in, and sudoers rule.
# Usage: sudo sh uninstall.sh [--purge]   (--purge also removes /var/lib/microduck-helper)
# Refuses if Studio currently holds the servo bus (bus-held flag present).
set -eu
[ "$(id -u)" -eq 0 ] || { echo "Requires root: sudo sh uninstall.sh" >&2; exit 1; }
if [ -e /run/microduck-helper/bus-held ]; then
    echo "Studio is holding the servo bus (/run/microduck-helper/bus-held present): hand back to robotd first" >&2
    exit 1
fi
if [ -e /etc/systemd/system/robotd.service.d/zzzzzz-studio-imu-recalibrate.conf ]; then
    echo "IMU recalibration session is still active: finish or cancel in Studio first" >&2
    exit 1
fi
if [ -e /var/lib/microduck-helper/imu-recalibrate/manifest.json ]; then
    /usr/bin/python3 -IS -c 'import json,sys;sys.exit(1 if json.load(open("/var/lib/microduck-helper/imu-recalibrate/manifest.json"))["active"] else 0)' || {
        echo "Unfinished or unreadable IMU recalibration session: recover session before uninstalling" >&2
        exit 1
    }
fi
rm -f /etc/sudoers.d/microduck-studio
rm -f /etc/systemd/system/robotd.service.d/zzzz-studio-bus.conf
systemctl daemon-reload
rm -f /usr/local/sbin/microduck-helper /usr/local/sbin/microduck-maintenance /usr/local/sbin/phone_tls.py
systemctl stop microduck-bus-hold 2>/dev/null || true
rm -rf /run/microduck-helper /run/microduck-handover
if [ "${1:-}" = "--purge" ]; then rm -rf /var/lib/microduck-helper; fi
echo "Uninstalled. robotd was not restarted; verify zzzz-studio-bus.conf is absent from DropInPaths."
