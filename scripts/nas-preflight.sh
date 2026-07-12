#!/bin/bash
set -euo pipefail

failures=0

report_failure() {
    echo "[ERROR] $*" >&2
    failures=$((failures + 1))
}

require_command() {
    local command_name="$1"
    if command -v "$command_name" >/dev/null 2>&1; then
        printf '%-12s %s\n' "$command_name:" "$(command -v "$command_name")"
    else
        printf '%-12s missing\n' "$command_name:"
        report_failure "required command is missing: $command_name"
    fi
}

echo "== CardPulse NAS preflight (read-only) =="
if [[ "${EUID}" -eq 0 ]]; then
    echo "root: yes"
else
    echo "root: no"
    report_failure "run this preflight with sudo"
fi

for command_name in systemctl python3 flock caddy timeout ss modinfo lsusb udevadm runuser; do
    require_command "$command_name"
done

if command -v systemctl >/dev/null 2>&1 \
    && ! systemctl show --property=Version --value >/dev/null 2>&1; then
    report_failure "systemctl is present but systemd is not running"
fi

if command -v python3 >/dev/null 2>&1; then
    if python3 -c 'import yaml' >/dev/null 2>&1; then
        echo "PyYAML: available"
    else
        report_failure "python3 with PyYAML is required"
    fi
fi

if command -v modinfo >/dev/null 2>&1; then
    if modinfo option >/dev/null 2>&1; then
        echo "option driver: available"
    else
        report_failure "option driver is unavailable"
    fi
fi

if command -v lsusb >/dev/null 2>&1; then
    if lsusb -d 2ca3:4006; then
        echo "QDC507 USB (2ca3:4006): detected"
    else
        echo "QDC507 USB (2ca3:4006): not detected (expected before physical cutover)"
    fi
fi

echo "serial devices:"
shopt -s nullglob
serial_devices=(/dev/ttyUSB* /dev/cardpulse-at)
if ((${#serial_devices[@]})); then
    ls -l "${serial_devices[@]}"
else
    echo "none"
fi

echo "CardPulse systemd units:"
if command -v systemctl >/dev/null 2>&1; then
    systemctl list-unit-files 'cardpulse*' --no-legend --no-pager || true
fi

echo "CardPulse cron entries:"
if command -v crontab >/dev/null 2>&1; then
    root_cron="$(crontab -l 2>/dev/null || :)"
    if grep -qi cardpulse <<<"$root_cron"; then
        echo "$root_cron" | grep -i cardpulse
        echo "[WARN] legacy CardPulse cron entries will be removed by nas-install.sh"
    else
        echo "none"
    fi
else
    echo "crontab is unavailable (no user cron scheduler installed)"
fi

if command -v ss >/dev/null 2>&1; then
    listener_lines="$(ss -H -ltn 'sport = :8766' 2>/dev/null || :)"
    if [[ -n "$listener_lines" ]]; then
        echo "listeners on 8766:"
        echo "$listener_lines"
        report_failure "port 8766 is already listening; resolve it before NAS installation"
    else
        echo "port 8766: available"
    fi
fi

if ((failures > 0)); then
    echo "NAS preflight failed: $failures required condition(s) are not satisfied." >&2
    exit 1
fi

echo "NAS preflight passed. QDC507 hardware may remain disconnected until physical cutover."
