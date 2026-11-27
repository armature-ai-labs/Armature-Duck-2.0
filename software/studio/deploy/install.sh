#!/bin/sh
# Install Studio root helper + robotd guard drop-in + sudoers rule.
# Usage (on the duck, inside deploy/): sudo sh install.sh [studio_user, default radxa]
# Does not restart robotd or touch /dev/ttyS2. See INSTALL.md.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
user=${1:-radxa}

[ "$(id -u)" -eq 0 ] || { echo "Requires root: sudo sh install.sh [user]" >&2; exit 1; }
case $user in
    *[!a-z0-9_-]*|"") echo "Username must match [a-z0-9_-]+: $user" >&2; exit 1 ;;
esac
id "$user" >/dev/null 2>&1 || { echo "User not found: $user" >&2; exit 1; }
[ -f /etc/systemd/system/robotd.service ] || echo "Note: /etc/systemd/system/robotd.service not found; drop-in will wait for unit" >&2

# 1. Root helpers: root:root 0755, verify parent dirs are root-owned and non-writable
install -o root -g root -m 0755 "$here/microduck-helper" /usr/local/sbin/microduck-helper
install -d -o root -g root -m 0755 /var/lib/microduck-helper /var/lib/microduck-helper/imu-recalibrate
for d in /usr /usr/local /usr/local/sbin; do
    if [ "$(stat -c %u "$d")" != 0 ]; then
        echo "Warning: $d is not owned by root; helper binary could be tampered with" >&2
    fi
    if [ -n "$(find "$d" -maxdepth 0 -perm /022 2>/dev/null)" ]; then
        echo "Warning: $d is group/world writable; helper binary could be tampered with" >&2
    fi
done

install -o root -g root -m 0644 "$here/phone_tls.py" /usr/local/sbin/phone_tls.py
install -o root -g root -m 0644 "$here/microduck-maintenance" /usr/local/sbin/microduck-maintenance   # Fixed maintenance helper for backup/restore/shutdown

# 2. robotd guard drop-in
install -d -o root -g root -m 0755 /etc/systemd/system/robotd.service.d
install -o root -g root -m 0644 "$here/zzzz-studio-bus.conf" /etc/systemd/system/robotd.service.d/zzzz-studio-bus.conf
systemctl daemon-reload

# 3. sudoers: allow only helper full path; validate with visudo -cf first
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
{
    echo "# Written by Microduck Studio deploy/install.sh; allows root helper only. See deploy/INSTALL.md"
    echo "$user ALL=(root) NOPASSWD: /usr/local/sbin/microduck-helper"
} >"$tmp"
visudo -cf "$tmp" >/dev/null
install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/microduck-studio

# 4. Verify
echo "--- Verify ---"
systemctl show robotd -p DropInPaths
ls -l /usr/local/sbin/microduck-helper /usr/local/sbin/microduck-maintenance /etc/sudoers.d/microduck-studio /etc/systemd/system/robotd.service.d/zzzz-studio-bus.conf
echo "Test as $user: echo '{}' | sudo -n /usr/local/sbin/microduck-helper bus-nope ; echo \$?   (expect 2: bad verb)"
echo "Installed. robotd was not restarted."
