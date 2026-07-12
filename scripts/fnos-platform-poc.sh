#!/bin/sh
set -eu

CONTAINER="${CARDPULSE_CONTAINER:-cardpulse}"
SOCKET_PATH="${CARDPULSE_FNOS_SOCKET:-}"
PKGVAR_PATH="${TRIM_PKGVAR:-}"
APPDEST_PATH="${TRIM_APPDEST:-}"
RECORD_PERSISTENCE=false
MARKER_PATH="/var/lib/cardpulse/state/fnos-platform-poc.json"

usage() {
    printf '%s\n' "Usage: $0 --socket <host-app.sock> [--container <name>] [--record-persistence]"
}

fail() {
    printf '%s\n' "fnOS platform POC failed: $*" >&2
    exit 1
}

# The image starts as root only long enough to inherit the package-owned data
# and gateway groups. Reuse that same effective identity for POC checks.
run_as_cardpulse() {
    docker exec --user root "$CONTAINER" /bin/sh -ceu '
        user_gid=$(id -g cardpulse)
        group_list="$user_gid"
        reject_dynamic_gid() {
            printf "%s\\n" "fnOS platform POC rejected unsafe dynamic GID for $1: $2" >&2
            exit 1
        }
        validate_dynamic_gid() {
            runtime_path=$1
            gid=$2
            case "$gid" in
                ""|*[!0-9]*) reject_dynamic_gid "$runtime_path" "missing or non-numeric value" ;;
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
        append_gid() {
            case ",$group_list," in
                *,"$1",*) ;;
                *) group_list="$group_list,$1" ;;
            esac
        }
        for runtime_path in /var/lib/cardpulse /run/cardpulse; do
            runtime_gid=$(stat -c "%g" "$runtime_path") \
                || reject_dynamic_gid "$runtime_path" "cannot read ownership"
            validate_dynamic_gid "$runtime_path" "$runtime_gid"
            append_gid "$runtime_gid"
        done
        exec setpriv --reuid=cardpulse --regid=cardpulse --groups="$group_list" --nnp "$@"
    ' cardpulse-platform-runtime "$@"
}

assert_nonroot_runtime_process() {
    process_fragment="$1"
    docker exec --user root "$CONTAINER" /bin/sh -ceu '
        expected_uid=$(id -u cardpulse)
        for process_dir in /proc/[0-9]*; do
            [ -r "$process_dir/status" ] || continue
            process_uid=$(awk "/^Uid:/{print \$2}" "$process_dir/status")
            [ "$process_uid" = "$expected_uid" ] || continue
            tr "\\000" " " < "$process_dir/cmdline" | grep -Fq "$1" || continue
            [ "$process_uid" -gt 0 ] 2>/dev/null || exit 1
            exit 0
        done
        exit 1
    ' cardpulse-platform-process "$process_fragment" \
        || fail "runtime process is not a non-root cardpulse process: $process_fragment"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --container)
            [ "$#" -ge 2 ] || fail "--container requires a value"
            CONTAINER="$2"
            shift 2
            ;;
        --socket)
            [ "$#" -ge 2 ] || fail "--socket requires a value"
            SOCKET_PATH="$2"
            shift 2
            ;;
        --record-persistence)
            RECORD_PERSISTENCE=true
            shift
            ;;
        --help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            fail "unknown option: $1"
            ;;
    esac
done

[ -n "$SOCKET_PATH" ] || fail "--socket is required"
[ -S "$SOCKET_PATH" ] || fail "gateway socket is not available: $SOCKET_PATH"
[ -n "$PKGVAR_PATH" ] || fail "TRIM_PKGVAR is required"
[ -n "$APPDEST_PATH" ] || fail "TRIM_APPDEST is required"
docker inspect "$CONTAINER" >/dev/null 2>&1 || fail "container is not available: $CONTAINER"

privileged=$(docker inspect --format '{{.HostConfig.Privileged}}' "$CONTAINER")
[ "$privileged" = "false" ] || fail "container must not be privileged"

network_mode=$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$CONTAINER")
[ "$network_mode" != "host" ] || fail "container must not use host networking"

cap_add=$(docker inspect --format '{{json .HostConfig.CapAdd}}' "$CONTAINER")
case "$cap_add" in
    null|"[]") ;;
    *) fail "container must not add Linux capabilities" ;;
esac

security_opt=$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' "$CONTAINER")
printf '%s\n' "$security_opt" | grep -Fq '"no-new-privileges:true"' \
    || fail "container must retain no-new-privileges:true"

device_mappings=$(docker inspect --format '{{range .HostConfig.Devices}}{{printf "%s\n" .PathOnHost}}{{end}}' "$CONTAINER")
[ -z "$device_mappings" ] || fail "no-device platform POC found a device mapping"

port_bindings=$(docker inspect --format '{{json .HostConfig.PortBindings}}' "$CONTAINER")
case "$port_bindings" in
    null|"{}") ;;
    *) fail "container has published TCP or UDP ports" ;;
esac

mount_paths=$(docker inspect --format '{{range .Mounts}}{{printf "%s:%s\n" .Source .Destination}}{{end}}' "$CONTAINER")
printf '%s\n' "$mount_paths" | grep -Fx "$PKGVAR_PATH:/var/lib/cardpulse" >/dev/null \
    || fail "private data mount does not originate from TRIM_PKGVAR"
printf '%s\n' "$mount_paths" | grep -Fx "$APPDEST_PATH:/run/cardpulse" >/dev/null \
    || fail "gateway socket mount does not originate from TRIM_APPDEST"
unexpected_mounts=$(printf '%s\n' "$mount_paths" | \
    sed '/^$/d' | grep -Ev '^[^:]+:/(var/lib/cardpulse|run/cardpulse)$' || true)
[ -z "$unexpected_mounts" ] || fail "unexpected host mount: $unexpected_mounts"

if ! docker exec --user root "$CONTAINER" /bin/sh -ceu '
    test "$(stat -c "%a" /run/cardpulse)" = 3770
' cardpulse-platform-socket-permissions; then
    fail "gateway socket directory must retain sticky and setgid permissions"
fi

if ! docker exec --user root "$CONTAINER" /bin/sh -ceu '
    for package_asset in \
        /run/cardpulse/docker/docker-compose.yaml \
        /run/cardpulse/ui/config \
        /run/cardpulse/diagnostics/fnos-platform-poc.sh; do
        test -e "$package_asset"
    done
' cardpulse-platform-package-assets; then
    fail "required package assets are missing"
fi

if ! run_as_cardpulse /bin/sh -ceu '
    for package_asset in \
        /run/cardpulse/docker/docker-compose.yaml \
        /run/cardpulse/ui/config \
        /run/cardpulse/diagnostics/fnos-platform-poc.sh; do
        test ! -w "$package_asset"
        test ! -w "$(dirname "$package_asset")"
    done
'; then
    fail "runtime cardpulse user can modify package assets"
fi

assert_nonroot_runtime_process "cardpulse_web.py"
assert_nonroot_runtime_process "fnos-scheduler.sh"

if curl --fail --silent --show-error --unix-socket "$SOCKET_PATH" \
    http://localhost/app/cardpulse/api/health >/dev/null 2>&1; then
    fail "backend socket accepted a request without fnOS administrator identity"
fi

backend_response=$(curl --fail --silent --show-error --unix-socket "$SOCKET_PATH" \
    -H "X-Trim-Isadmin: true" \
    http://localhost/app/cardpulse/api/health)
printf '%s' "$backend_response" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' \
    || fail "backend health check did not succeed"

if [ "$RECORD_PERSISTENCE" = true ]; then
    completed_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    run_as_cardpulse /bin/sh -ceu '
        umask 077
        marker="/var/lib/cardpulse/state/fnos-platform-poc.json"
        completed_at="$1"
        printf "%s%s%s\\n" \
            "{\"schema_version\":1,\"socket_backend\":true,\"completed_at\":\"" \
            "$completed_at" \
            "\"}" > "$marker"
        chmod 600 "$marker"
    ' cardpulse-platform-write "$completed_at"
    run_as_cardpulse /bin/sh -ceu '
        marker="/var/lib/cardpulse/state/fnos-platform-poc.json"
        completed_at="$1"
        test -f "$marker"
        test "$(stat -c "%a" "$marker")" = 600
        grep -Fq "\"completed_at\":\"$completed_at\"" "$marker"
    ' cardpulse-platform-readback "$completed_at"
    printf '%s\n' "fnOS platform persistence marker recorded at $MARKER_PATH"
fi

printf '%s\n' "fnOS platform POC completed; direct socket checks do not validate the fnOS HTTPS gateway, administrator isolation, or header sanitization."
