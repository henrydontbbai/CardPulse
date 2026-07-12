#!/bin/sh
set -eu

# This runs before an App Center upgrade can start the replacement container.
# Keep the existing private configuration and scheduler settings, while making
# the one safety-critical value explicit for the next runtime.
config_path="${TRIM_PKGVAR:?TRIM_PKGVAR is required}/config/config.yaml"

[ ! -L "$config_path" ] || {
    printf '%s\n' "CardPulse refused an unsafe configuration object during upgrade." >&2
    exit 1
}
[ ! -e "$config_path" ] || [ -f "$config_path" ] || {
    printf '%s\n' "CardPulse refused an unsafe configuration object during upgrade." >&2
    exit 1
}
[ -f "$config_path" ] || exit 0

temporary_path="${config_path}.cardpulse-disarm.$$"
cleanup() {
    rm -f "$temporary_path"
}
trap cleanup EXIT HUP INT TERM

# config.yaml is shared only between the package lifecycle account and the
# runtime cardpulse account through the private package group.
umask 007
awk '
    function ensure_scheduler_disabled() {
        if (in_scheduler && !wrote_enabled) {
            print "  enabled: false"
            wrote_enabled = 1
        }
    }
    /^scheduler:[[:space:]]*(#.*)?$/ {
        ensure_scheduler_disabled()
        saw_scheduler = 1
        in_scheduler = 1
        wrote_enabled = 0
        print
        next
    }
    in_scheduler {
        if ($0 ~ /^[^[:space:]#]/) {
            ensure_scheduler_disabled()
            in_scheduler = 0
        } else if ($0 ~ /^  enabled:[[:space:]]*/) {
            print "  enabled: false"
            wrote_enabled = 1
            next
        }
    }
    { print }
    END {
        ensure_scheduler_disabled()
        if (!saw_scheduler) {
            print ""
            print "scheduler:"
            print "  enabled: false"
        }
    }
' "$config_path" > "$temporary_path"

chmod 660 "$temporary_path"
mv "$temporary_path" "$config_path"
trap - EXIT HUP INT TERM
