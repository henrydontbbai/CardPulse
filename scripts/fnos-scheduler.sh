#!/bin/sh
set -eu

CONFIG_PATH="${CARDPULSE_CONFIG_DIR:-/var/lib/cardpulse/config}/config.yaml"
INTERVAL_SECONDS="${CARDPULSE_SCHEDULER_POLL_SECONDS:-3600}"
ACCEPTANCE_PATH="${CARDPULSE_QDC507_ACCEPTANCE_PATH:-/var/lib/cardpulse/lifecycle/qdc507-readonly-acceptance.json}"
CARDPULSE_LIB_DIR="${CARDPULSE_LIB_DIR:-/opt/cardpulse/lib}"
RUNTIME_DISARMED="${CARDPULSE_SCHEDULER_RUNTIME_DISARMED:-false}"

scheduler_enabled() {
    if [ "$RUNTIME_DISARMED" != "true" ]; then
        printf '%s\n' false
        return 0
    fi

    CARDPULSE_LIB_DIR="$CARDPULSE_LIB_DIR" CONFIG_PATH="$CONFIG_PATH" ACCEPTANCE_PATH="$ACCEPTANCE_PATH" python3 - <<'PY'
import os
import sys
from pathlib import Path

library_dir = os.environ["CARDPULSE_LIB_DIR"]
if library_dir not in sys.path:
    sys.path.insert(0, library_dir)

try:
    from cardpulse_web import (
        keepalive_config_error,
        qdc507_readonly_acceptance_status,
        scheduler_enabled_from_config,
    )

    config_path = Path(os.environ["CONFIG_PATH"])
    acceptance_path = Path(os.environ["ACCEPTANCE_PATH"])
    accepted, _reason = qdc507_readonly_acceptance_status(acceptance_path)
    enabled = (
        scheduler_enabled_from_config(config_path)
        and not keepalive_config_error(config_path)
        and accepted
    )
except Exception:
    enabled = False

print("true" if enabled else "false")
PY
}

case "${1:-}" in
--check)
    scheduler_enabled
    exit 0
    ;;
"")
    ;;
*)
    printf '%s\n' "Usage: $0 [--check]" >&2
    exit 2
    ;;
esac

while :; do
    if [ "$(scheduler_enabled)" = "true" ]; then
        cardpulse || true
    fi
    sleep "$INTERVAL_SECONDS" &
    wait $!
done
