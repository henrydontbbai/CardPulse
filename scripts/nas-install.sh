#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_DIR="$ROOT_DIR/deploy/nas"
INSTALL_ROOT="/opt/cardpulse"
STATE_ROOT="/var/lib/cardpulse"
WEB_ENV="/etc/cardpulse/web.env"
DRY_RUN=false
PUBLIC_ORIGIN=""
CADDY_HOSTNAME=""

usage() {
    cat <<'EOF'
Usage: sudo bash scripts/nas-install.sh --public-origin https://cardpulse.nas [--dry-run]
EOF
}

run() {
    if "$DRY_RUN"; then
        printf '[dry-run] '
        printf '%q ' "$@"
        printf '\n'
        return
    fi
    "$@"
}

require_command() {
    local command_name="$1"
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "$command_name is required for NAS deployment" >&2
        exit 1
    }
}

remove_cardpulse_crontab() {
    local account="$1"
    local current_cron
    local filtered_cron

    if [[ "$account" == "root" ]]; then
        current_cron="$(crontab -l 2>/dev/null || :)"
    else
        current_cron="$(crontab -u cardpulse -l 2>/dev/null || :)"
    fi
    if ! grep -qi cardpulse <<<"$current_cron"; then
        return 0
    fi

    filtered_cron="$(grep -vi cardpulse <<<"$current_cron" || :)"
    if [[ "$account" == "root" ]]; then
        printf '%s\n' "$filtered_cron" | crontab -
    else
        printf '%s\n' "$filtered_cron" | crontab -u cardpulse -
    fi
}

remove_cardpulse_cron_file() {
    local cron_file="$1"
    local temporary_path

    [[ -e "$cron_file" ]] || return 0
    [[ ! -L "$cron_file" && -f "$cron_file" ]] || {
        echo "legacy CardPulse cron path is unsafe: $cron_file" >&2
        return 1
    }
    grep -qi cardpulse "$cron_file" || return 0
    temporary_path="$(mktemp "$(dirname "$cron_file")/.${cron_file##*/}.XXXXXX")"
    if ! grep -vi cardpulse "$cron_file" > "$temporary_path"; then
        :
    fi
    chmod --reference="$cron_file" "$temporary_path"
    chown --reference="$cron_file" "$temporary_path"
    mv -f "$temporary_path" "$cron_file"
}

legacy_cardpulse_cron_exists() {
    local current_cron
    if command -v crontab >/dev/null 2>&1; then
        current_cron="$(crontab -l 2>/dev/null || :)"
        grep -qi cardpulse <<<"$current_cron" && return 0
        current_cron="$(crontab -u cardpulse -l 2>/dev/null || :)"
        grep -qi cardpulse <<<"$current_cron" && return 0
    fi
    grep -qi cardpulse /etc/crontab 2>/dev/null && return 0
    grep -Rqi cardpulse /etc/cron.d 2>/dev/null && return 0
    return 1
}

remove_legacy_cardpulse_cron() {
    local cron_file

    if command -v crontab >/dev/null 2>&1; then
        remove_cardpulse_crontab root || {
            echo "failed to remove legacy CardPulse root cron entry" >&2
            return 1
        }
        remove_cardpulse_crontab cardpulse || {
            echo "failed to remove legacy CardPulse cardpulse cron entry" >&2
            return 1
        }
    fi
    remove_cardpulse_cron_file /etc/crontab || return 1
    if [[ -d /etc/cron.d ]]; then
        for cron_file in /etc/cron.d/*; do
            remove_cardpulse_cron_file "$cron_file" || return 1
        done
    fi
    if legacy_cardpulse_cron_exists; then
        echo "legacy CardPulse cron entry remains after removal" >&2
        return 1
    fi
    echo "removed legacy CardPulse cron entries"
}

ensure_web_auth_config() {
    local auth_file="$STATE_ROOT/config/web-auth.json"

    if "$DRY_RUN"; then
        printf '[dry-run] require a valid Web password at %q before exposing services\n' "$auth_file"
        return
    fi
    [[ ! -L "$auth_file" ]] || {
        echo "Web password file must not be a symbolic link" >&2
        exit 1
    }
    if [[ ! -f "$auth_file" ]]; then
        [[ -t 0 && -t 1 ]] || {
            echo "initial NAS installation needs an interactive terminal to set the Web password" >&2
            exit 1
        }
        echo "Set the CardPulse Web password before HTTPS services are started."
        runuser -u cardpulse -- /usr/bin/python3 "$INSTALL_ROOT/scripts/cardpulse-web-password.py" --auth-file "$auth_file"
    fi
    PYTHONPATH="$INSTALL_ROOT/lib" /usr/bin/python3 - "$auth_file" <<'PY'
import sys
from pathlib import Path

from web_auth import password_config_is_valid

if not password_config_is_valid(Path(sys.argv[1])):
    raise SystemExit("Web password configuration is missing or invalid")
PY
    chown cardpulse:cardpulse "$auth_file"
    chmod 0600 "$auth_file"
}

validate_public_origin() {
    local origin_parts=()
    local validated
    if ! validated="$(python3 "$SCRIPT_DIR/nas-origin.py" "$PUBLIC_ORIGIN")"; then
        echo "invalid HTTPS public origin" >&2
        exit 2
    fi
    mapfile -t origin_parts <<<"$validated"
    [[ "${#origin_parts[@]}" -eq 2 ]] || {
        echo "invalid HTTPS public origin" >&2
        exit 2
    }
    PUBLIC_ORIGIN="${origin_parts[0]}"
    CADDY_HOSTNAME="${origin_parts[1]}"
}

render_caddyfile() {
    local temporary_path
    if "$DRY_RUN"; then
        printf '[dry-run] render Caddyfile for %q\n' "$CADDY_HOSTNAME"
        return
    fi
    temporary_path="$(mktemp /etc/caddy/Caddyfile.XXXXXX)"
    python3 - "$DEPLOY_DIR/Caddyfile" "$temporary_path" "$CADDY_HOSTNAME" <<'PY'
from pathlib import Path
import sys

template = Path(sys.argv[1]).read_text(encoding="utf-8")
hostname = sys.argv[3]
if template.count("__CARDPULSE_NAS_HOSTNAME__") != 1:
    raise SystemExit("invalid CardPulse Caddyfile template")
Path(sys.argv[2]).write_text(
    template.replace("__CARDPULSE_NAS_HOSTNAME__", hostname),
    encoding="utf-8",
)
PY
    chmod 0644 "$temporary_path"
    chown root:root "$temporary_path"
    mv -f "$temporary_path" /etc/caddy/Caddyfile
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --public-origin)
            PUBLIC_ORIGIN="${2:-}"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

[[ "${EUID}" -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -n "$PUBLIC_ORIGIN" ]] || { echo "--public-origin is required" >&2; exit 2; }
for command_name in systemctl python3 flock caddy timeout udevadm install getent id groupadd useradd runuser; do
    require_command "$command_name"
done
systemctl show --property=Version --value >/dev/null 2>&1 || {
    echo "a running systemd host is required" >&2
    exit 1
}
python3 -c 'import yaml' >/dev/null 2>&1 || {
    echo "python3 with PyYAML is required" >&2
    exit 1
}
validate_public_origin

run install -d -m 0755 "$INSTALL_ROOT/bin" "$INSTALL_ROOT/lib" "$INSTALL_ROOT/scripts" "$INSTALL_ROOT/web" /etc/cardpulse /etc/udev/rules.d /etc/systemd/system /etc/caddy /usr/local/lib/cardpulse
if ! getent group cardpulse >/dev/null 2>&1; then
    run groupadd --system cardpulse
fi
if ! id cardpulse >/dev/null 2>&1; then
    run useradd --system --gid cardpulse --home "$STATE_ROOT" --shell /usr/sbin/nologin cardpulse
fi
run install -d -o cardpulse -g cardpulse -m 0700 "$STATE_ROOT/config" "$STATE_ROOT/state"
run install -m 0755 "$ROOT_DIR/bin/cardpulse" "$INSTALL_ROOT/bin/cardpulse"
run install -m 0644 "$ROOT_DIR/lib/"*.sh "$ROOT_DIR/lib/"*.py "$INSTALL_ROOT/lib/"
run install -m 0755 "$ROOT_DIR/scripts/cardpulse-web.py" "$INSTALL_ROOT/scripts/cardpulse-web.py"
run install -m 0755 "$ROOT_DIR/scripts/cardpulse-web-password.py" "$INSTALL_ROOT/scripts/cardpulse-web-password.py"
run install -m 0755 "$ROOT_DIR/scripts/nas-origin.py" "$INSTALL_ROOT/scripts/nas-origin.py"
run install -m 0644 "$ROOT_DIR/web/index.html" "$INSTALL_ROOT/web/index.html"
run install -m 0755 "$INSTALL_ROOT/bin/cardpulse" /usr/local/bin/cardpulse

if [[ ! -f "$STATE_ROOT/config/config.yaml" ]]; then
    if ! "$DRY_RUN"; then
        cat > "$STATE_ROOT/config/config.yaml" <<'EOF'
serial:
  port: /dev/cardpulse-at
  baudrate: 115200
  auto_detect: false
sms:
  phone: ""
  message: ""
  interval_days: 179
  timeout: 30
retry:
  max_attempts: 1
  interval: 1
notify:
  enabled: false
EOF
    fi
fi
run chown cardpulse:cardpulse "$STATE_ROOT/config/config.yaml"
run chmod 0600 "$STATE_ROOT/config/config.yaml"

run install -m 0644 "$DEPLOY_DIR/99-cardpulse-qdc507.rules" /etc/udev/rules.d/99-cardpulse-qdc507.rules
for unit in cardpulse-modem-ready.service cardpulse.service cardpulse.timer cardpulse-web.service; do
    run install -m 0644 "$DEPLOY_DIR/$unit" "/etc/systemd/system/$unit"
done
render_caddyfile
run install -m 0755 "$SCRIPT_DIR/nas-modem-prepare.sh" /usr/local/lib/cardpulse/nas-modem-prepare.sh

if ! "$DRY_RUN"; then
    printf 'CARDPULSE_WEB_PUBLIC_ORIGIN=%q\n' "$PUBLIC_ORIGIN" > "$WEB_ENV"
    chmod 0600 "$WEB_ENV"
    chown root:root "$WEB_ENV"
fi

if ! "$DRY_RUN"; then
    remove_legacy_cardpulse_cron
fi
ensure_web_auth_config
run udevadm control --reload-rules
run systemctl daemon-reload
run systemctl disable --now cardpulse.timer
run systemctl enable cardpulse-web.service caddy.service
run systemctl restart cardpulse-web.service
run systemctl reload-or-restart caddy.service
echo "NAS assets installed. cardpulse.timer remains disabled until migration and hardware acceptance succeed."
