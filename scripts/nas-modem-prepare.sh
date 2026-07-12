#!/bin/bash
set -euo pipefail

MODEM_VENDOR="2ca3"
MODEM_PRODUCT="4006"
AT_ALIAS="/dev/cardpulse-at"
LOCK_FILE="/run/cardpulse-modem-prepare.lock"
SYS_CLASS_TTY_ROOT="${CARDPULSE_SYS_CLASS_TTY_ROOT:-/sys/class/tty}"

fail() {
    echo "[ERROR] $*" >&2
    exit 1
}

require_root() {
    [[ "${EUID}" -eq 0 ]] || fail "run as root"
}

send_at() {
    local port="$1"
    local command="$2"
    timeout 4 bash -c '
        port="$1"
        command="$2"
        stty -F "$port" 115200 raw -echo
        printf "%s\r" "$command" > "$port"
        timeout 3 cat "$port"
    ' bash "$port" "$command" 2>/dev/null || true
}

port_belongs_to_qdc507() {
    local port="$1"
    local tty_name="${port##*/}"
    local current
    local parent
    local vendor
    local product

    current="$(readlink -f "$SYS_CLASS_TTY_ROOT/$tty_name/device" 2>/dev/null || true)"
    while [[ -n "$current" && "$current" != "/" ]]; do
        if [[ -r "$current/idVendor" && -r "$current/idProduct" ]]; then
            vendor="$(tr '[:upper:]' '[:lower:]' < "$current/idVendor")"
            product="$(tr '[:upper:]' '[:lower:]' < "$current/idProduct")"
            if [[ "$vendor" == "$MODEM_VENDOR" && "$product" == "$MODEM_PRODUCT" ]]; then
                return 0
            fi
        fi
        parent="$(dirname "$current")"
        [[ "$parent" != "$current" ]] || break
        current="$parent"
    done
    return 1
}

port_is_ready() {
    local port="$1"
    local at_output
    local sim_output
    local reg_output

    at_output="$(send_at "$port" "AT")"
    grep -q "OK" <<<"$at_output" || return 1
    sim_output="$(send_at "$port" "AT+CPIN?")"
    grep -q "SIM READY" <<<"$sim_output" || return 1
    reg_output="$(send_at "$port" "AT+CREG?")"
    grep -Eq '\+CREG: [0-9]+,(1|5)' <<<"$reg_output" || return 1
    return 0
}

main() {
    require_root
    exec 9>"$LOCK_FILE"
    flock -x 9

    modprobe option
    if [[ -w /sys/bus/usb-serial/drivers/option1/new_id ]]; then
        printf '%s %s\n' "$MODEM_VENDOR" "$MODEM_PRODUCT" > /sys/bus/usb-serial/drivers/option1/new_id || true
    fi

    local port
    for _ in $(seq 1 20); do
        for port in /dev/ttyUSB*; do
            [[ -e "$port" ]] || continue
            if port_belongs_to_qdc507 "$port" && port_is_ready "$port"; then
                ln -sfn "$port" "${AT_ALIAS}.tmp"
                mv -Tf "${AT_ALIAS}.tmp" "$AT_ALIAS"
                chgrp cardpulse "$AT_ALIAS" || true
                echo "CardPulse AT port ready: $port"
                return 0
            fi
        done
        sleep 1
    done
    fail "no QDC507 AT port passed AT, SIM READY, and CREG checks"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
