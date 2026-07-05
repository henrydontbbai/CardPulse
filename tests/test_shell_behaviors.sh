#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

fail() {
    echo "[FAIL] $*" >&2
    exit 1
}

expect_failure_contains() {
    local expected="$1"
    shift
    local output
    if output=$("$@" 2>&1); then
        fail "command unexpectedly succeeded: $*"
    fi
    if [[ "$output" != *"$expected"* ]]; then
        echo "$output" >&2
        fail "expected output to contain: $expected"
    fi
}

expect_failure_contains "需要参数" bash bin/cardpulse --config
expect_failure_contains "需要参数" bash bin/cardpulse --notify-channel
expect_failure_contains "未知通知渠道" bash bin/cardpulse --notify --notify-channel invalid
expect_failure_contains "--notify-channel 需要与 --notify 一起使用" bash bin/cardpulse --notify-channel telegram

grep -q 'acquire_singleton_lock' bin/cardpulse || fail "CLI missing singleton lock helper"
grep -q '未找到 flock' bin/cardpulse || fail "CLI missing macOS flock fallback warning"
if grep -q 'rm -f "${CARDPULSE_LOCK_FILE}"' bin/cardpulse; then
    fail "CLI must not remove singleton lock file on exit"
fi
grep -q 'command -v flock' lib/state_manager.sh || fail "state manager missing flock availability guard"

if grep -R '\(\(errors++\)\)' lib bin scripts >/dev/null; then
    fail "found fragile error counter increment pattern"
fi

if grep -R '\(\([a-zA-Z_][a-zA-Z0-9_]*++\)\)' lib bin scripts >/dev/null; then
    fail "found fragile arithmetic post-increment pattern"
fi

if grep -E "trap .*[[:space:]]RETURN" lib/notifier.sh >/dev/null; then
    fail "notifier should not install RETURN traps for temporary cleanup"
fi

if grep -R ': -[0-9]' lib bin scripts >/dev/null; then
    fail "found Bash-version-sensitive negative substring offset"
fi

grep -q 'WANT_SYSTEMD=true' scripts/install.sh || fail "installer scheduler defaults missing WANT_SYSTEMD"
grep -q 'WANT_CRON=auto' scripts/install.sh || fail "installer scheduler defaults missing WANT_CRON=auto"
grep -q -- '--no-systemd' scripts/install.sh || fail "installer missing --no-systemd flag"
grep -q -- '--no-cron' scripts/install.sh || fail "installer missing --no-cron flag"
grep -q -- '--with-cron' scripts/install.sh || fail "installer missing --with-cron flag"
grep -q 'setup_scheduler' scripts/install.sh || fail "installer missing setup_scheduler"

grep -q 'at_configure_stty' lib/at_modem.sh || fail "AT modem missing portable stty helper"
grep -q 'gtimeout' lib/at_modem.sh || fail "AT modem missing macOS gtimeout support"
grep -q '/dev/cu.usbserial\*' lib/at_modem.sh || fail "AT modem missing macOS USB serial detection"

echo "shell behavior tests ok"
