#!/bin/sh
set -eu

legacy_found=0

report_legacy() {
    printf '%s\n' "$1" >&2
    legacy_found=1
}

path_exists() {
    [ -e "$1" ] || [ -L "$1" ]
}

check_native_path() {
    native_path=$1
    if path_exists "$native_path"; then
        report_legacy "legacy native path detected: $native_path"
    fi
}

check_native_unit() {
    unit=$1

    for unit_path in \
        "/etc/systemd/system/$unit" \
        "/usr/lib/systemd/system/$unit" \
        "/lib/systemd/system/$unit"; do
        if path_exists "$unit_path"; then
            report_legacy "legacy native systemd unit path detected: $unit_path"
        fi
    done

    if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet "$unit" 2>/dev/null; then
            report_legacy "legacy native systemd unit is active: $unit"
        fi
        if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
            report_legacy "legacy native systemd unit is enabled: $unit"
        fi
    fi
}

check_crontab() {
    user=$1

    command -v crontab >/dev/null 2>&1 || return 0
    cron_entries=$(crontab -u "$user" -l 2>/dev/null || :)
    if printf '%s\n' "$cron_entries" | grep -qi 'cardpulse'; then
        report_legacy "legacy CardPulse cron entry detected for user: $user"
    fi
}

check_cron_file() {
    cron_file=$1

    if [ -f "$cron_file" ] && grep -qi 'cardpulse' "$cron_file"; then
        report_legacy "legacy CardPulse cron entry detected in: $cron_file"
    fi
}

check_native_path /opt/cardpulse
check_native_path /var/lib/cardpulse

for unit in \
    cardpulse.timer \
    cardpulse.service \
    cardpulse-web.service \
    cardpulse-modem-ready.service; do
    check_native_unit "$unit"
done

check_crontab root
check_crontab cardpulse
check_cron_file /etc/crontab

if [ -d /etc/cron.d ]; then
    for cron_file in /etc/cron.d/*; do
        [ -f "$cron_file" ] || continue
        check_cron_file "$cron_file"
    done
fi

if [ "$legacy_found" -ne 0 ]; then
    cat >&2 <<'EOF'
CardPulse FPK refused to continue because legacy native scheduling artifacts are present.
Manual migration required:
  1. Back up any legacy CardPulse configuration and state.
  2. An administrator must manually stop and disable only the listed legacy
     CardPulse systemd units, then remove or archive their unit files.
  3. An administrator must manually remove the listed CardPulse cron entries.
  4. Re-run the FPK install or upgrade after confirming no native CardPulse
     scheduler remains.
The FPK did not modify host services, cron jobs, modem drivers, udev rules, or data.
EOF
    exit 1
fi
