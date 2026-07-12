#!/bin/bash
set -eu
umask 077

DATA_DIR="${CARDPULSE_DATA_DIR:-/var/lib/cardpulse}"
CONFIG_DIR="$DATA_DIR/config"
STATE_DIR="$DATA_DIR/state"
CONFIG_PATH="$CONFIG_DIR/config.yaml"
CONTROL_DIR="$DATA_DIR/lifecycle"
CONTROL_ACTIVE_MODE_FILE="$CONTROL_DIR/qdc507-device-mode.active"
SOCKET_PATH="${CARDPULSE_WEB_SOCKET:-/run/cardpulse/app.sock}"
SOCKET_DIR=$(dirname "$SOCKET_PATH")
CARDPULSE_QDC507_DEVICE_MODE_PATH="$CONTROL_DIR/qdc507-device-mode.active"

validate_lifecycle_control_for_runtime() {
    runtime_uid=$(id -u 2>/dev/null) || return 1
    data_uid=$(stat -c '%u' "$DATA_DIR") || return 1
    data_gid=$(stat -c '%g' "$DATA_DIR") || return 1

    if [ -L "$CONTROL_DIR" ] || [ ! -d "$CONTROL_DIR" ]; then
        return 1
    fi
    control_uid=$(stat -c '%u' "$CONTROL_DIR") || return 1
    control_gid=$(stat -c '%g' "$CONTROL_DIR") || return 1
    control_mode=$(stat -c '%a' "$CONTROL_DIR") || return 1
    if [ "$control_uid" != "$data_uid" ] || [ "$control_uid" = "$runtime_uid" ] || \
        [ "$control_gid" != "$data_gid" ] || [ "$control_mode" != "2750" ]; then
        return 1
    fi

    if [ -L "$CONTROL_ACTIVE_MODE_FILE" ] || [ ! -f "$CONTROL_ACTIVE_MODE_FILE" ]; then
        return 1
    fi
    active_uid=$(stat -c '%u' "$CONTROL_ACTIVE_MODE_FILE") || return 1
    active_gid=$(stat -c '%g' "$CONTROL_ACTIVE_MODE_FILE") || return 1
    active_mode=$(stat -c '%a' "$CONTROL_ACTIVE_MODE_FILE") || return 1
    if [ "$active_uid" != "$control_uid" ] || [ "$active_gid" != "$data_gid" ] || \
        [ "$active_mode" != "640" ]; then
        return 1
    fi
}

if [ "$(id -u)" -eq 0 ]; then
    mkdir -p "$STATE_DIR" "$SOCKET_DIR"
    socket_gid=$(stat -c '%g' "$SOCKET_DIR") || {
        printf '%s\n' "CardPulse fnOS runtime rejected unsafe dynamic GID for $SOCKET_DIR: cannot read ownership" >&2
        exit 1
    }
    data_gid=$(stat -c '%g' "$DATA_DIR") || {
        printf '%s\n' "CardPulse fnOS runtime rejected unsafe dynamic GID for $DATA_DIR: cannot read ownership" >&2
        exit 1
    }
    data_uid=$(stat -c '%u' "$DATA_DIR") || {
        printf '%s\n' "CardPulse fnOS runtime rejected unsafe configuration directory: cannot read ownership" >&2
        exit 1
    }

    reject_dynamic_gid() {
        printf '%s\n' "CardPulse fnOS runtime rejected unsafe dynamic GID for $1: $2" >&2
        exit 1
    }

    validate_dynamic_gid() {
        runtime_path=$1
        gid=$2
        case "$gid" in
            ''|*[!0-9]*) reject_dynamic_gid "$runtime_path" "missing or non-numeric value" ;;
            0|0[0]*) reject_dynamic_gid "$runtime_path" "GID 0 is prohibited" ;;
        esac
        command -v getent >/dev/null 2>&1 \
            || reject_dynamic_gid "$runtime_path" "getent is unavailable"
        if group_entry=$(getent group "$gid" 2>/dev/null); then
            while IFS=: read -r group_name _; do
                case "$group_name" in
                    cardpulse|dialout) ;;
                    *) reject_dynamic_gid "$runtime_path" "image group $group_name (GID $gid) is prohibited" ;;
                esac
            done <<EOF
$group_entry
EOF
        else
            group_status=$?
            [ "$group_status" -eq 2 ] \
                || reject_dynamic_gid "$runtime_path" "cannot resolve image group for GID $gid"
        fi
    }

    validate_dynamic_gid "$DATA_DIR" "$data_gid"
    validate_dynamic_gid "$SOCKET_DIR" "$socket_gid"
    if [ -e /dev/cardpulse-at ]; then
        device_gid=$(stat -c '%g' /dev/cardpulse-at) || {
            printf '%s\n' "CardPulse fnOS runtime rejected unsafe dynamic GID for /dev/cardpulse-at: cannot read ownership" >&2
            exit 1
        }
        validate_dynamic_gid /dev/cardpulse-at "$device_gid"
    fi

    chmod 3770 "$DATA_DIR"
    chmod 2770 "$STATE_DIR"
    chmod 3770 "$SOCKET_DIR"
    if [ -L "$CONFIG_DIR" ] || [ ! -d "$CONFIG_DIR" ]; then
        printf '%s\n' "CardPulse fnOS runtime rejected unsafe configuration directory: $CONFIG_DIR" >&2
        exit 1
    fi
    config_uid=$(stat -c '%u' "$CONFIG_DIR") || exit 1
    config_gid=$(stat -c '%g' "$CONFIG_DIR") || exit 1
    config_mode=$(stat -c '%a' "$CONFIG_DIR") || exit 1
    if [ "$config_uid" != "$data_uid" ] || [ "$config_gid" != "$data_gid" ] || \
        [ "$config_mode" != "2750" ]; then
        printf '%s\n' "CardPulse fnOS runtime rejected unsafe configuration directory: $CONFIG_DIR" >&2
        exit 1
    fi
    if [ -e "$CONFIG_PATH" ] || [ -L "$CONFIG_PATH" ]; then
        if [ -L "$CONFIG_PATH" ] || [ ! -f "$CONFIG_PATH" ]; then
            printf '%s\n' "CardPulse fnOS runtime rejected unsafe configuration object: $CONFIG_PATH" >&2
            exit 1
        fi
    fi
    if [ ! -f "$CONFIG_PATH" ]; then
        cp /opt/cardpulse/config/config.fnos.example.yaml "$CONFIG_PATH"
    fi
    # config/ is lifecycle-owned. config.yaml is the one group-shared file.
    chgrp "$data_gid" "$CONFIG_PATH"
    chmod 660 "$CONFIG_PATH"
    if ! grep -q '^scheduler:' "$CONFIG_PATH"; then
        printf '\nscheduler:\n  enabled: false\n' >> "$CONFIG_PATH"
    fi
    if [ -d "$SOCKET_DIR/docker" ]; then
        chmod go-rwx "$SOCKET_DIR/docker"
        for compose_file in "$SOCKET_DIR/docker"/docker-compose*.yaml; do
            [ -f "$compose_file" ] || continue
            chmod go-rwx "$compose_file"
        done
    fi

    group_list=$(id -g cardpulse)
    append_group() {
        case ",$group_list," in
            *,"$1",*) ;;
            *) group_list="$group_list,$1" ;;
        esac
    }
    append_group "$data_gid"
    append_group "$socket_gid"
    if [ -n "${device_gid:-}" ]; then
        append_group "$device_gid"
    fi
    exec setpriv --reuid=cardpulse --regid=cardpulse --groups="$group_list" --nnp "$0" "$@"
fi

mkdir -p "$STATE_DIR" "$SOCKET_DIR"
chmod 2770 "$STATE_DIR" 2>/dev/null || true

if ! validate_lifecycle_control_for_runtime; then
    # Do not follow or repair a lifecycle object that the container might have
    # created. /dev/null is a stable non-regular sentinel, so Web and the
    # scheduler fail closed until a lifecycle callback restores a trusted file.
    CARDPULSE_QDC507_DEVICE_MODE_PATH="/dev/null"
    printf '%s\n' "CardPulse fnOS runtime rejected unsafe lifecycle control state; QDC507 remains degraded." >&2
fi

if [ -L "$CONFIG_PATH" ] || { [ -e "$CONFIG_PATH" ] && [ ! -f "$CONFIG_PATH" ]; }; then
    printf '%s\n' "CardPulse fnOS runtime rejected unsafe configuration object: $CONFIG_PATH" >&2
    exit 1
fi

disable_scheduler_for_runtime_start() {
    CONFIG_PATH="$CONFIG_PATH" python3 - <<'PY'
import os
from pathlib import Path

import yaml

path = Path(os.environ["CONFIG_PATH"])
try:
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
except (OSError, yaml.YAMLError):
    raise SystemExit(1)
if not isinstance(config, dict):
    raise SystemExit(1)
config.setdefault("scheduler", {})["enabled"] = False
path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
PY
}

CARDPULSE_SCHEDULER_RUNTIME_DISARMED=false
if disable_scheduler_for_runtime_start; then
    CARDPULSE_SCHEDULER_RUNTIME_DISARMED=true
else
    printf '%s\n' "CardPulse refused to arm its scheduler because startup disarm failed." >&2
fi

export CARDPULSE_CONFIG_DIR="$CONFIG_DIR"
export CARDPULSE_STATE_DIR="$STATE_DIR"
export CARDPULSE_WEB_SOCKET="$SOCKET_PATH"
export CARDPULSE_QDC507_ACCEPTANCE_PATH="${CARDPULSE_QDC507_ACCEPTANCE_PATH:-$CONTROL_DIR/qdc507-readonly-acceptance.json}"
export CARDPULSE_QDC507_DEVICE_MODE_PATH
export CARDPULSE_SCHEDULER_RUNTIME_DISARMED

if [ "$#" -gt 0 ]; then
    exec /usr/local/bin/cardpulse "$@"
fi

python3 /opt/cardpulse/lib/cardpulse_web.py \
    --socket "$SOCKET_PATH" \
    --base-path "${CARDPULSE_WEB_BASE_PATH:-/app/cardpulse}" \
    --fnos-gateway &
web_pid=$!

/opt/cardpulse/scripts/fnos-scheduler.sh &
scheduler_pid=$!

stop_children() {
    kill "$scheduler_pid" "$web_pid" 2>/dev/null || true
    wait "$scheduler_pid" 2>/dev/null || true
    wait "$web_pid" 2>/dev/null || true
}

trap 'stop_children; exit 0' INT TERM
if wait -n "$web_pid" "$scheduler_pid"; then
    :
fi
stop_children
exit 1
