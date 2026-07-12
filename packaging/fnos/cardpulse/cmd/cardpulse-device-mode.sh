#!/bin/sh

# This file is sourced by package lifecycle scripts. Production selection only
# accepts the fixed host alias and real tty sysfs root verified during QDC507
# provisioning. The parameterized identity helper below exists for local tests.
CARDPULSE_DEVICE_NODE="/dev/cardpulse-at"
CARDPULSE_SYSFS_TTY_ROOT="/sys/class/tty"
QDC507_USB_ID="2ca3:4006"
readonly CARDPULSE_DEVICE_NODE CARDPULSE_SYSFS_TTY_ROOT QDC507_USB_ID

cardpulse_use_system_path() {
    # Device selection runs host tools; do not resolve them from an inherited,
    # mutable PATH. The gateway status branch does not call this helper.
    PATH=/usr/sbin:/usr/bin:/sbin:/bin
    export PATH
}

cardpulse_device_mode_paths() {
    CONTROL_DIR="${TRIM_PKGVAR}/lifecycle"
    REQUESTED_MODE_FILE="$CONTROL_DIR/qdc507-device-mode.requested"
    ACTIVE_MODE_FILE="$CONTROL_DIR/qdc507-device-mode.active"
    COMPOSE_DIR="${TRIM_APPDEST}/docker"
    ACTIVE_COMPOSE="$COMPOSE_DIR/docker-compose.yaml"
    NO_DEVICE_COMPOSE="$COMPOSE_DIR/docker-compose.no-device.yaml"
    DEVICE_COMPOSE="$COMPOSE_DIR/docker-compose.device.yaml"
}

cardpulse_device_mode_log() {
    if [ -n "${TRIM_TEMP_LOGFILE:-}" ]; then
        printf '%s\n' "$1" > "$TRIM_TEMP_LOGFILE"
    fi
}

cardpulse_path_exists_without_following() {
    [ -e "$1" ] || [ -L "$1" ]
}

cardpulse_lifecycle_uid() {
    id -u 2>/dev/null
}

cardpulse_lifecycle_control_dir_is_trusted() {
    expected_uid=$1
    expected_gid=$2

    if [ -L "$CONTROL_DIR" ] || [ ! -d "$CONTROL_DIR" ]; then
        cardpulse_device_mode_log "CardPulse rejected unsafe lifecycle control directory; CardPulse remains in no-device mode."
        return 1
    fi

    control_uid=$(stat -c '%u' "$CONTROL_DIR" 2>/dev/null) || return 1
    control_gid=$(stat -c '%g' "$CONTROL_DIR" 2>/dev/null) || return 1
    control_mode=$(stat -c '%a' "$CONTROL_DIR" 2>/dev/null) || return 1
    if [ "$control_uid" != "$expected_uid" ] || \
        [ "$control_gid" != "$expected_gid" ] || [ "$control_mode" != "2750" ]; then
        cardpulse_device_mode_log "CardPulse rejected unsafe lifecycle control directory; CardPulse remains in no-device mode."
        return 1
    fi
}

cardpulse_prepare_lifecycle_control_dir() {
    cardpulse_device_mode_paths

    if [ -z "${TRIM_PKGVAR:-}" ] || [ -L "$TRIM_PKGVAR" ] || [ ! -d "$TRIM_PKGVAR" ]; then
        cardpulse_device_mode_log "CardPulse rejected unsafe package private directory; CardPulse remains in no-device mode."
        return 1
    fi

    lifecycle_uid=$(cardpulse_lifecycle_uid) || {
        cardpulse_device_mode_log "CardPulse could not determine lifecycle ownership; CardPulse remains in no-device mode."
        return 1
    }
    package_uid=$(stat -c '%u' "$TRIM_PKGVAR" 2>/dev/null) || return 1
    if [ "$package_uid" != "$lifecycle_uid" ]; then
        cardpulse_device_mode_log "CardPulse rejected unsafe package private directory; CardPulse remains in no-device mode."
        return 1
    fi

    # The shared parent remains writable for normal data, but sticky ownership
    # prevents the container user from replacing the lifecycle control entry.
    if ! chmod 3770 "$TRIM_PKGVAR"; then
        cardpulse_device_mode_log "CardPulse could not protect its package private directory; CardPulse remains in no-device mode."
        return 1
    fi
    package_gid=$(stat -c '%g' "$TRIM_PKGVAR" 2>/dev/null) || return 1
    package_mode=$(stat -c '%a' "$TRIM_PKGVAR" 2>/dev/null) || return 1
    if [ "$package_mode" != "3770" ]; then
        cardpulse_device_mode_log "CardPulse could not protect its package private directory; CardPulse remains in no-device mode."
        return 1
    fi

    if ! cardpulse_path_exists_without_following "$CONTROL_DIR"; then
        if ! (umask 027; mkdir -m 2750 "$CONTROL_DIR"); then
            cardpulse_device_mode_log "CardPulse could not create its protected lifecycle control directory; CardPulse remains in no-device mode."
            return 1
        fi
    fi

    cardpulse_lifecycle_control_dir_is_trusted "$lifecycle_uid" "$package_gid"
}

# Return 0 for a verified regular file, 2 when it does not exist, and 1 for a
# legacy or malformed object. In the latter case, never chmod, read, or follow it.
cardpulse_lifecycle_control_file_status() {
    file_path=$1
    expected_mode=$2

    if ! cardpulse_path_exists_without_following "$file_path"; then
        return 2
    fi
    if [ -L "$file_path" ] || [ ! -f "$file_path" ]; then
        cardpulse_device_mode_log "CardPulse rejected unsafe lifecycle control file; CardPulse remains in no-device mode."
        return 1
    fi

    expected_uid=$(cardpulse_lifecycle_uid) || return 1
    expected_gid=$(stat -c '%g' "$CONTROL_DIR" 2>/dev/null) || return 1
    file_uid=$(stat -c '%u' "$file_path" 2>/dev/null) || return 1
    file_gid=$(stat -c '%g' "$file_path" 2>/dev/null) || return 1
    file_mode=$(stat -c '%a' "$file_path" 2>/dev/null) || return 1
    if [ "$file_uid" != "$expected_uid" ] || \
        [ "$file_gid" != "$expected_gid" ] || [ "$file_mode" != "$expected_mode" ]; then
        cardpulse_device_mode_log "CardPulse rejected unsafe lifecycle control file; CardPulse remains in no-device mode."
        return 1
    fi
}

cardpulse_write_device_mode_file() {
    file_path=$1
    value=$2
    expected_mode=$3

    cardpulse_prepare_lifecycle_control_dir || return 1
    if cardpulse_lifecycle_control_file_status "$file_path" "$expected_mode"; then
        file_status=0
    else
        file_status=$?
    fi
    case "$file_status" in
    0)
        # The containing directory and existing regular file have just been
        # verified; the container cannot replace either of them.
        printf '%s\n' "$value" > "$file_path" || return 1
        ;;
    2)
        file_name=${file_path##*/}
        temporary_path="$CONTROL_DIR/.${file_name}.cardpulse.$$"
        if ! (umask 077; set -C; : > "$temporary_path"); then
            cardpulse_device_mode_log "CardPulse could not create its lifecycle control file; CardPulse remains in no-device mode."
            return 1
        fi
        if ! printf '%s\n' "$value" > "$temporary_path" || ! chmod "$expected_mode" "$temporary_path" || \
            ! mv -f "$temporary_path" "$file_path"; then
            rm -f "$temporary_path"
            cardpulse_device_mode_log "CardPulse could not write its lifecycle control file; CardPulse remains in no-device mode."
            return 1
        fi
        cardpulse_lifecycle_control_file_status "$file_path" "$expected_mode" || return 1
        ;;
    *)
        return 1
        ;;
    esac
}

cardpulse_requested_device_mode() {
    if cardpulse_lifecycle_control_file_status "$REQUESTED_MODE_FILE" "600"; then
        requested_status=0
    else
        requested_status=$?
    fi
    if [ "$requested_status" -ne 0 ]; then
        printf '%s\n' "disabled"
        return 0
    fi

    mode=$(cat "$REQUESTED_MODE_FILE" 2>/dev/null || true)
    case "$mode" in
    enabled|disabled)
        printf '%s\n' "$mode"
        ;;
    *)
        printf '%s\n' "disabled"
        ;;
    esac
}

cardpulse_hex_to_decimal() {
    hex_value=$1
    decimal_value=0

    [ -n "$hex_value" ] || return 1
    while [ -n "$hex_value" ]; do
        hex_digit=${hex_value%"${hex_value#?}"}
        hex_value=${hex_value#?}
        case "$hex_digit" in
        0) digit_value=0 ;;
        1) digit_value=1 ;;
        2) digit_value=2 ;;
        3) digit_value=3 ;;
        4) digit_value=4 ;;
        5) digit_value=5 ;;
        6) digit_value=6 ;;
        7) digit_value=7 ;;
        8) digit_value=8 ;;
        9) digit_value=9 ;;
        a|A) digit_value=10 ;;
        b|B) digit_value=11 ;;
        c|C) digit_value=12 ;;
        d|D) digit_value=13 ;;
        e|E) digit_value=14 ;;
        f|F) digit_value=15 ;;
        *) return 1 ;;
        esac
        decimal_value=$((decimal_value * 16 + digit_value))
    done

    printf '%s\n' "$decimal_value"
}

cardpulse_device_major_minor_for_path() {
    device_node=$1
    rdev_hex=$(stat -c '%t:%T' "$device_node" 2>/dev/null) || return 1
    case "$rdev_hex" in
    *:*) ;;
    *) return 1 ;;
    esac

    major_hex=${rdev_hex%%:*}
    minor_hex=${rdev_hex#*:}
    major_decimal=$(cardpulse_hex_to_decimal "$major_hex") || return 1
    minor_decimal=$(cardpulse_hex_to_decimal "$minor_hex") || return 1
    printf '%s:%s\n' "$major_decimal" "$minor_decimal"
}

cardpulse_tty_major_minor_is_valid() {
    tty_major_minor=$1
    case "$tty_major_minor" in
    *[!0-9:]*|*:*:*|:*|*:|"") return 1 ;;
    *) return 0 ;;
    esac
}

# This helper accepts paths only for hermetic local tests. Production callers
# use cardpulse_current_qdc507_identity below, which supplies readonly values.
cardpulse_qdc507_identity_for_paths() {
    device_node=$1
    sysfs_tty_root=$2

    if [ ! -c "$device_node" ]; then
        cardpulse_device_mode_log "QDC507 device /dev/cardpulse-at is not available as a character device; CardPulse remains in no-device mode."
        return 1
    fi

    major_minor=$(cardpulse_device_major_minor_for_path "$device_node") || {
        cardpulse_device_mode_log "QDC507 device /dev/cardpulse-at could not be inspected for major:minor; CardPulse remains in no-device mode."
        return 1
    }
    if [ ! -d "$sysfs_tty_root" ]; then
        cardpulse_device_mode_log "QDC507 device /dev/cardpulse-at cannot find the tty sysfs root; CardPulse remains in no-device mode."
        return 1
    fi

    resolved_device=$(readlink -f "$device_node" 2>/dev/null) || {
        cardpulse_device_mode_log "QDC507 device /dev/cardpulse-at cannot resolve its current tty; CardPulse remains in no-device mode."
        return 1
    }
    case "$resolved_device" in
    /dev/*)
        tty_name=${resolved_device#/dev/}
        ;;
    *)
        cardpulse_device_mode_log "QDC507 device /dev/cardpulse-at resolved outside /dev; CardPulse remains in no-device mode."
        return 1
        ;;
    esac
    case "$tty_name" in
    ''|*/*|*[!A-Za-z0-9._-]*)
        cardpulse_device_mode_log "QDC507 device /dev/cardpulse-at did not resolve to a valid /dev/<tty>; CardPulse remains in no-device mode."
        return 1
        ;;
    esac

    matching_tty_count=0
    matching_tty_name=""
    for tty_dev_file in "$sysfs_tty_root"/*/dev; do
        [ -r "$tty_dev_file" ] || continue
        candidate_tty_dir=${tty_dev_file%/dev}
        candidate_tty_name=${candidate_tty_dir##*/}
        case "$candidate_tty_name" in
        ''|*[!A-Za-z0-9._-]*) continue ;;
        esac
        candidate_major_minor=$(tr -d '[:space:]' < "$tty_dev_file") || continue
        cardpulse_tty_major_minor_is_valid "$candidate_major_minor" || continue
        if [ "$candidate_major_minor" = "$major_minor" ]; then
            matching_tty_count=$((matching_tty_count + 1))
            matching_tty_name=$candidate_tty_name
        fi
    done

    if [ "$matching_tty_count" -eq 0 ]; then
        cardpulse_device_mode_log "QDC507 device /dev/cardpulse-at is absent from tty sysfs; CardPulse remains in no-device mode."
        return 1
    fi
    if [ "$matching_tty_count" -ne 1 ]; then
        cardpulse_device_mode_log "QDC507 device /dev/cardpulse-at matches multiple tty sysfs nodes; CardPulse remains in no-device mode."
        return 1
    fi
    if [ "$matching_tty_name" != "$tty_name" ]; then
        cardpulse_device_mode_log "QDC507 device /dev/cardpulse-at resolved tty does not match its unique sysfs node; CardPulse remains in no-device mode."
        return 1
    fi

    sysfs_path=$(readlink -f "$sysfs_tty_root/$matching_tty_name/device" 2>/dev/null) || {
        cardpulse_device_mode_log "QDC507 device /dev/cardpulse-at cannot resolve its tty sysfs path; CardPulse remains in no-device mode."
        return 1
    }
    if [ ! -d "$sysfs_path" ]; then
        cardpulse_device_mode_log "QDC507 device /dev/cardpulse-at has no valid tty sysfs path; CardPulse remains in no-device mode."
        return 1
    fi

    while [ "$sysfs_path" != "/" ]; do
        if [ -r "$sysfs_path/idVendor" ] && [ -r "$sysfs_path/idProduct" ]; then
            vendor=$(tr -d '[:space:]' < "$sysfs_path/idVendor" | tr '[:upper:]' '[:lower:]')
            product=$(tr -d '[:space:]' < "$sysfs_path/idProduct" | tr '[:upper:]' '[:lower:]')
            if [ "$vendor:$product" = "$QDC507_USB_ID" ]; then
                return 0
            fi
            cardpulse_device_mode_log "QDC507 device /dev/cardpulse-at has USB identity $vendor:$product, expected $QDC507_USB_ID; CardPulse remains in no-device mode."
            return 1
        fi

        parent_path=${sysfs_path%/*}
        [ -n "$parent_path" ] || parent_path="/"
        [ "$parent_path" != "$sysfs_path" ] || break
        sysfs_path=$parent_path
    done

    cardpulse_device_mode_log "QDC507 device /dev/cardpulse-at has no readable USB idVendor/idProduct; CardPulse remains in no-device mode."
    return 1
}

cardpulse_current_qdc507_identity() {
    cardpulse_qdc507_identity_for_paths "$CARDPULSE_DEVICE_NODE" "$CARDPULSE_SYSFS_TTY_ROOT"
}

cardpulse_activate_compose_template() {
    template=$1
    mode_label=$2
    temporary_compose="$COMPOSE_DIR/.docker-compose.yaml.cardpulse.$$"

    if ! cp "$template" "$temporary_compose"; then
        cardpulse_device_mode_log "CardPulse could not prepare its safe $mode_label mode."
        return 1
    fi
    if ! mv -f "$temporary_compose" "$ACTIVE_COMPOSE"; then
        rm -f "$temporary_compose"
        cardpulse_device_mode_log "CardPulse could not activate its safe $mode_label mode."
        return 1
    fi
}

select_cardpulse_device_mode() {
    cardpulse_use_system_path
    cardpulse_device_mode_paths

    # A failed replacement must stop the lifecycle: otherwise a previous
    # device overlay could survive into a later platform start.
    cardpulse_activate_compose_template "$NO_DEVICE_COMPOSE" "no-device" || return 1
    cardpulse_prepare_lifecycle_control_dir || return 0

    requested_mode=$(cardpulse_requested_device_mode)
    if [ "$requested_mode" = "enabled" ] && cardpulse_current_qdc507_identity; then
        if cardpulse_write_device_mode_file "$ACTIVE_MODE_FILE" "enabled" "640"; then
            if cardpulse_activate_compose_template "$DEVICE_COMPOSE" "device"; then
                return 0
            fi
            cardpulse_write_device_mode_file "$ACTIVE_MODE_FILE" "degraded" "640" || true
            return 1
        fi
        return 0
    fi

    cardpulse_write_device_mode_file "$ACTIVE_MODE_FILE" "degraded" "640" || true
    return 0
}

configure_cardpulse_device_mode_from_wizard() {
    cardpulse_use_system_path
    cardpulse_device_mode_paths
    requested_mode="${wizard_qdc507_device_mode:-disabled}"

    case "$requested_mode" in
    enabled|disabled)
        ;;
    *)
        requested_mode="disabled"
        cardpulse_device_mode_log "Invalid QDC507 device mode was rejected; CardPulse remains in no-device mode."
        ;;
    esac

    cardpulse_prepare_lifecycle_control_dir && \
        cardpulse_write_device_mode_file "$REQUESTED_MODE_FILE" "$requested_mode" "600" || true
    select_cardpulse_device_mode
}
