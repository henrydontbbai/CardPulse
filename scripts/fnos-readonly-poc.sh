#!/bin/sh
set -eu

CONTAINER="${CARDPULSE_CONTAINER:-cardpulse}"
SOCKET_PATH="${CARDPULSE_FNOS_SOCKET:-}"
REQUIRE_DEVICE=false
RECORD_ACCEPTANCE=false
MARKER_PATH="/var/lib/cardpulse/lifecycle/qdc507-readonly-acceptance.json"
QDC507_USB_ID="2ca3:4006"
RESOLVED_DEVICE=""
RUNTIME_VERSION=""
RUNTIME_IMAGE=""
PACKAGE_VERSION=""

usage() {
    printf '%s\n' "Usage: $0 --socket <host-app.sock> [--container <name>] [--require-device] [--record-acceptance]"
}

fail() {
    printf '%s\n' "fnOS QDC507 POC failed: $*" >&2
    exit 1
}

# Match the runtime's dynamic data, gateway, and device GIDs without running
# diagnostics or persistent-state checks as root.
run_as_cardpulse() {
    docker exec --user root "$CONTAINER" /bin/sh -ceu '
        user_gid=$(id -g cardpulse)
        group_list="$user_gid"
        reject_dynamic_gid() {
            printf "%s\\n" "fnOS QDC507 POC rejected unsafe dynamic GID for $1: $2" >&2
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
        for runtime_path in /var/lib/cardpulse /run/cardpulse /dev/cardpulse-at; do
            runtime_gid=$(stat -c "%g" "$runtime_path") \
                || reject_dynamic_gid "$runtime_path" "cannot read ownership"
            validate_dynamic_gid "$runtime_path" "$runtime_gid"
            append_gid "$runtime_gid"
        done
        exec setpriv --reuid=cardpulse --regid=cardpulse --groups="$group_list" --nnp "$@"
    ' cardpulse-runtime "$@"
}

resolve_qdc507_identity() {
    RESOLVED_DEVICE=$(readlink -f /dev/cardpulse-at 2>/dev/null) \
        || fail "cannot resolve the fixed QDC507 device alias"
    case "$RESOLVED_DEVICE" in
        /dev/*) ;;
        *) fail "fixed QDC507 device alias resolved outside /dev" ;;
    esac
    case "$RESOLVED_DEVICE" in
        *[!A-Za-z0-9._/-]*) fail "fixed QDC507 device alias contains unsafe characters" ;;
    esac

    tty_name=${RESOLVED_DEVICE##*/}
    sysfs_path=$(readlink -f "/sys/class/tty/$tty_name/device" 2>/dev/null) \
        || fail "cannot resolve QDC507 sysfs path for $RESOLVED_DEVICE"
    while [ "$sysfs_path" != "/" ]; do
        if [ -r "$sysfs_path/idVendor" ] && [ -r "$sysfs_path/idProduct" ]; then
            vendor=$(tr -d '[:space:]' < "$sysfs_path/idVendor" | tr '[:upper:]' '[:lower:]')
            product=$(tr -d '[:space:]' < "$sysfs_path/idProduct" | tr '[:upper:]' '[:lower:]')
            usb_id="$vendor:$product"
            [ "$usb_id" = "$QDC507_USB_ID" ] \
                || fail "fixed device is not QDC507 ($usb_id, expected $QDC507_USB_ID)"
            return 0
        fi
        parent_path=$(dirname "$sysfs_path")
        [ "$parent_path" != "$sysfs_path" ] || break
        sysfs_path="$parent_path"
    done
    fail "QDC507 USB identity could not be read from sysfs"
}

read_runtime_release_identity() {
    if ! container_environment=$(docker inspect --format '{{range .Config.Env}}{{printf "%s\n" .}}{{end}}' "$CONTAINER"); then
        fail "container release identity cannot be read"
    fi
    RUNTIME_VERSION=$(printf '%s\n' "$container_environment" | sed -n 's/^CARDPULSE_RUNTIME_VERSION=//p')
    RUNTIME_IMAGE=$(printf '%s\n' "$container_environment" | sed -n 's/^CARDPULSE_RUNTIME_IMAGE=//p')
    PACKAGE_VERSION=$(printf '%s\n' "$container_environment" | sed -n 's/^CARDPULSE_FPK_VERSION=//p')
    printf '%s' "$RUNTIME_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
        || fail "container runtime version is missing or invalid"
    printf '%s' "$RUNTIME_IMAGE" | grep -Eq '^[^[:space:]@]+@sha256:[0-9a-fA-F]{64}$' \
        || fail "container runtime image is missing or invalid"
    printf '%s' "$PACKAGE_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
        || fail "container package version is missing or invalid"
    [ "$PACKAGE_VERSION" = "$RUNTIME_VERSION" ] \
        || fail "container package version does not match runtime version"
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
        --require-device)
            REQUIRE_DEVICE=true
            shift
            ;;
        --record-acceptance)
            RECORD_ACCEPTANCE=true
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
[ "$REQUIRE_DEVICE" = true ] || fail "--require-device is required for the QDC507 read-only POC"
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

mount_paths=$(docker inspect --format '{{range .Mounts}}{{printf "%s:%s\\n" .Source .Destination}}{{end}}' "$CONTAINER")
if printf '%s\n' "$mount_paths" | grep -Eq '(^|:)/dev(/|$)'; then
    fail "container has a prohibited /dev or USB bind mount"
fi

mount_destinations=$(docker inspect --format '{{range .Mounts}}{{printf "%s\\n" .Destination}}{{end}}' "$CONTAINER")
for expected_mount in /var/lib/cardpulse /run/cardpulse; do
    printf '%s\n' "$mount_destinations" | grep -Fx "$expected_mount" >/dev/null \
        || fail "container is missing required mount destination: $expected_mount"
done
unexpected_mounts=$(printf '%s\n' "$mount_destinations" | \
    sed '/^$/d' | grep -Ev '^/(var/lib/cardpulse|run/cardpulse)$' || true)
[ -z "$unexpected_mounts" ] || fail "unexpected host mount destination: $unexpected_mounts"

if ! docker exec --user root "$CONTAINER" /bin/sh -ceu '
    for package_asset in \
        /run/cardpulse/docker/docker-compose.yaml \
        /run/cardpulse/ui/config \
        /run/cardpulse/diagnostics/fnos-readonly-poc.sh; do
        test -e "$package_asset"
    done
' cardpulse-package-assets; then
    fail "required package assets are missing"
fi

if ! run_as_cardpulse /bin/sh -ceu '
    for package_asset in \
        /run/cardpulse/docker/docker-compose.yaml \
        /run/cardpulse/ui/config \
        /run/cardpulse/diagnostics/fnos-readonly-poc.sh; do
        test ! -w "$package_asset"
        test ! -w "$(dirname "$package_asset")"
    done
'; then
    fail "runtime cardpulse user can modify package assets"
fi

device_mapping=$(docker inspect --format '{{range .HostConfig.Devices}}{{printf "%s:%s:%s\\n" .PathOnHost .PathInContainer .CgroupPermissions}}{{end}}' "$CONTAINER")
[ "$device_mapping" = "/dev/cardpulse-at:/dev/cardpulse-at:rwm" ] || fail "container device mapping is not exact"

port_bindings=$(docker inspect --format '{{json .HostConfig.PortBindings}}' "$CONTAINER")
case "$port_bindings" in
    null|"{}") ;;
    *) fail "container has published TCP or UDP ports" ;;
esac

if curl --fail --silent --show-error --unix-socket "$SOCKET_PATH" \
    http://localhost/app/cardpulse/api/health >/dev/null 2>&1; then
    fail "gateway socket accepted a request without fnOS administrator identity"
fi

backend_response=$(curl --fail --silent --show-error --unix-socket "$SOCKET_PATH" \
    -H "X-Trim-Isadmin: true" \
    http://localhost/app/cardpulse/api/health)
printf '%s' "$backend_response" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' \
    || fail "backend health check did not succeed"

if [ "$REQUIRE_DEVICE" = true ]; then
    runtime_uid=$(run_as_cardpulse id -u)
    [ "$runtime_uid" -gt 0 ] 2>/dev/null \
        || fail "diagnostic commands did not run as a non-root cardpulse user"
    run_as_cardpulse test -c /dev/cardpulse-at \
        && run_as_cardpulse test -r /dev/cardpulse-at \
        && run_as_cardpulse test -w /dev/cardpulse-at \
        || fail "cardpulse user cannot read and write the fixed device"
fi

for command in --doctor --info --sms-status --status; do
    run_as_cardpulse /usr/local/bin/cardpulse "$command"
done

if [ "$RECORD_ACCEPTANCE" = true ]; then
    [ "$REQUIRE_DEVICE" = true ] || fail "--record-acceptance requires --require-device"
    resolve_qdc507_identity
    read_runtime_release_identity
    completed_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    acceptance_json=$(printf '{"schema_version":3,"device":"/dev/cardpulse-at","resolved_device":"%s","usb_id":"%s","runtime_version":"%s","image_reference":"%s","package_version":"%s","commands":["--doctor","--info","--sms-status","--status"],"nonroot":true,"socket_backend":true,"device_mode":"enabled","completed_at":"%s"}' \
        "$RESOLVED_DEVICE" "$QDC507_USB_ID" "$RUNTIME_VERSION" "$RUNTIME_IMAGE" "$PACKAGE_VERSION" "$completed_at")
    if ! docker exec --user root "$CONTAINER" /bin/sh -ceu '
        marker="/var/lib/cardpulse/lifecycle/qdc507-readonly-acceptance.json"
        lifecycle_dir=${marker%/*}
        test ! -L "$lifecycle_dir"
        test -d "$lifecycle_dir"
        lifecycle_uid=$(stat -c "%u" "$lifecycle_dir")
        lifecycle_gid=$(stat -c "%g" "$lifecycle_dir")
        test "$(stat -c "%a" "$lifecycle_dir")" = 2750
        test "$lifecycle_uid" != "$(id -u cardpulse)"

        if test -e "$marker" || test -L "$marker"; then
            test ! -L "$marker"
            test -f "$marker"
            test "$(stat -c "%u" "$marker")" = "$lifecycle_uid"
            test "$(stat -c "%g" "$marker")" = "$lifecycle_gid"
            test "$(stat -c "%a" "$marker")" = 640
        fi

        temporary_marker="$lifecycle_dir/.qdc507-readonly-acceptance.$$"
        umask 077
        : > "$temporary_marker"
        printf "%s\\n" "$1" > "$temporary_marker"
        chown "$lifecycle_uid:$lifecycle_gid" "$temporary_marker"
        chmod 640 "$temporary_marker"
        mv -f "$temporary_marker" "$marker"
    ' cardpulse-acceptance-write "$acceptance_json"; then
        fail "could not record lifecycle-owned QDC507 acceptance"
    fi
    run_as_cardpulse /bin/sh -ceu '
        marker="/var/lib/cardpulse/lifecycle/qdc507-readonly-acceptance.json"
        completed_at="$1"
        resolved_device="$2"
        runtime_version="$3"
        runtime_image="$4"
        package_version="$5"
        test -f "$marker"
        test "$(stat -c "%a" "$marker")" = 640
        test -r "$marker"
        test ! -w "$marker"
        grep -Fq "\"completed_at\":\"$completed_at\"" "$marker"
        grep -Fq "\"resolved_device\":\"$resolved_device\"" "$marker"
        grep -Fq "\"usb_id\":\"2ca3:4006\"" "$marker"
        grep -Fq "\"runtime_version\":\"$runtime_version\"" "$marker"
        grep -Fq "\"image_reference\":\"$runtime_image\"" "$marker"
        grep -Fq "\"package_version\":\"$package_version\"" "$marker"
    ' cardpulse-acceptance-readback "$completed_at" "$RESOLVED_DEVICE" "$RUNTIME_VERSION" "$RUNTIME_IMAGE" "$PACKAGE_VERSION"
    printf '%s\n' "QDC507 read-only acceptance recorded at $MARKER_PATH"
fi

printf '%s\n' "fnOS QDC507 read-only POC completed"
