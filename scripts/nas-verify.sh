#!/bin/bash
set -euo pipefail

CONFIG_DIR="${CARDPULSE_CONFIG_DIR:-/var/lib/cardpulse/config}"
STATE_DIR="${CARDPULSE_STATE_DIR:-/var/lib/cardpulse/state}"
WEB_ENV="/etc/cardpulse/web.env"
AT_ALIAS="/dev/cardpulse-at"

fail() {
    echo "[ERROR] $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command is missing: $1"
}

require_private_directory() {
    local path="$1"
    [[ -d "$path" ]] || fail "required directory is missing: $path"
    [[ "$(stat -c '%a' "$path")" == "700" ]] || fail "directory must use mode 0700: $path"
    [[ "$(stat -c '%U:%G' "$path")" == "cardpulse:cardpulse" ]] || {
        fail "directory must be owned by cardpulse:cardpulse: $path"
    }
}

require_private_file() {
    local path="$1"
    [[ -f "$path" ]] || fail "required file is missing: $path"
    [[ "$(stat -c '%a' "$path")" == "600" ]] || fail "file must use mode 0600: $path"
    [[ "$(stat -c '%U:%G' "$path")" == "cardpulse:cardpulse" ]] || {
        fail "file must be owned by cardpulse:cardpulse: $path"
    }
}

assert_no_cardpulse_cron() {
    local user="$1"
    command -v crontab >/dev/null 2>&1 || return 0
    if crontab -u "$user" -l 2>/dev/null | grep -qi cardpulse; then
        fail "CardPulse cron entry remains for user $user"
    fi
}

run_as_cardpulse() {
    runuser -u cardpulse -- env \
        CARDPULSE_CONFIG_DIR="$CONFIG_DIR" \
        CARDPULSE_STATE_DIR="$STATE_DIR" \
        /usr/local/bin/cardpulse "$@"
}

echo "== CardPulse NAS verification (non-destructive) =="
[[ "${EUID}" -eq 0 ]] || fail "run as root"
for command_name in systemctl caddy ss stat readlink runuser python3; do
    require_command "$command_name"
done
id cardpulse >/dev/null 2>&1 || fail "cardpulse service account is missing"

require_private_directory "$CONFIG_DIR"
require_private_directory "$STATE_DIR"
require_private_file "$CONFIG_DIR/config.yaml"
require_private_file "$CONFIG_DIR/web-auth.json"
for state_file in messages.jsonl sms-operations.jsonl local-history.jsonl web-state.json recovery.json; do
    if [[ -e "$STATE_DIR/$state_file" ]]; then
        require_private_file "$STATE_DIR/$state_file"
    fi
done

python3 - "$CONFIG_DIR/config.yaml" <<'PY'
from pathlib import Path
import sys

import yaml

config = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
serial = config.get("serial", {}) if isinstance(config, dict) else {}
if not isinstance(serial, dict) or serial.get("port") != "/dev/cardpulse-at":
    raise SystemExit("config.yaml must use serial.port: /dev/cardpulse-at")
if serial.get("auto_detect") is not False:
    raise SystemExit("config.yaml must use serial.auto_detect: false")
PY

systemctl is-enabled --quiet cardpulse-web.service || fail "cardpulse-web.service is not enabled"
systemctl is-active --quiet cardpulse-web.service || fail "cardpulse-web.service is not active"
systemctl is-enabled --quiet caddy.service || fail "caddy.service is not enabled"
systemctl is-active --quiet caddy.service || fail "caddy.service is not active"
caddy validate --config /etc/caddy/Caddyfile

mapfile -t timer_units < <(
    systemctl list-unit-files 'cardpulse*.timer' --no-legend --no-pager | awk '{print $1}'
)
if [[ "${#timer_units[@]}" -ne 1 || "${timer_units[0]}" != "cardpulse.timer" ]]; then
    fail "exactly one CardPulse timer unit must be installed"
fi
if systemctl is-enabled --quiet cardpulse.timer; then
    fail "CardPulse timer must remain disabled until migration and hardware acceptance succeed"
fi
echo "cardpulse.timer: installed and disabled"

assert_no_cardpulse_cron root
assert_no_cardpulse_cron cardpulse
if [[ -f /etc/crontab ]] && grep -qi cardpulse /etc/crontab; then
    fail "CardPulse cron entry remains in /etc/crontab"
fi
if [[ -d /etc/cron.d ]] && grep -Rqi cardpulse /etc/cron.d; then
    fail "CardPulse cron entry remains in /etc/cron.d"
fi

[[ -f "$WEB_ENV" ]] || fail "Web Origin environment file is missing"
grep -Eq '^CARDPULSE_WEB_PUBLIC_ORIGIN=https://[^[:space:]]+$' "$WEB_ENV" || {
    fail "Web Origin environment file must contain one HTTPS public origin"
}

[[ -L "$AT_ALIAS" ]] || fail "stable AT alias is missing: $AT_ALIAS"
at_port="$(readlink -f "$AT_ALIAS")"
[[ -c "$at_port" ]] || fail "stable AT alias does not resolve to a serial device: $AT_ALIAS"
runuser -u cardpulse -- test -r "$at_port" || fail "cardpulse cannot read AT serial device: $at_port"
runuser -u cardpulse -- test -w "$at_port" || fail "cardpulse cannot write AT serial device: $at_port"
echo "AT port: $at_port"

listener_lines="$(ss -H -ltnp 'sport = :8766')"
grep -Eq '(^|[[:space:]])127\.0\.0\.1:8766([[:space:]]|$)' <<<"$listener_lines" || {
    fail "CardPulse Web is not listening on 127.0.0.1:8766"
}
if grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\[::\]|::):8766([[:space:]]|$)' <<<"$listener_lines"; then
    fail "CardPulse Web must not listen outside loopback"
fi

run_as_cardpulse --doctor
run_as_cardpulse --info
run_as_cardpulse --sms-status
run_as_cardpulse --status

echo "NAS verification passed. cardpulse.timer remains disabled."
