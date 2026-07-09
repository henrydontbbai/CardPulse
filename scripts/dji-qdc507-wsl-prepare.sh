#!/usr/bin/env bash
set -euo pipefail

VID="${CARDPULSE_DJI_VID:-2ca3}"
PID="${CARDPULSE_DJI_PID:-4006}"
SYS_ROOT="${CARDPULSE_SYS_ROOT:-/sys}"
DEV_ROOT="${CARDPULSE_DEV_ROOT:-/dev}"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "[ERROR] Run as root inside WSL: sudo bash scripts/dji-qdc507-wsl-prepare.sh" >&2
    exit 1
fi

find_usb_device() {
    local dev
    local vendor
    local product

    for dev in "$SYS_ROOT"/bus/usb/devices/*; do
        [[ -r "$dev/idVendor" && -r "$dev/idProduct" ]] || continue
        vendor="$(tr '[:upper:]' '[:lower:]' < "$dev/idVendor")"
        product="$(tr '[:upper:]' '[:lower:]' < "$dev/idProduct")"
        if [[ "$vendor" == "$VID" && "$product" == "$PID" ]]; then
            echo "$dev"
            return 0
        fi
    done
    return 1
}

list_serial_ports() {
    compgen -G "$DEV_ROOT/ttyUSB*" || true
}

device_path="$(find_usb_device || true)"
if [[ -z "$device_path" ]]; then
    echo "[ERROR] DJI/Baiwang USB device ${VID}:${PID} was not found in WSL." >&2
    echo "[INFO] Attach it first from Windows: usbipd attach --wsl --busid <BUSID>" >&2
    exit 2
fi

echo "[INFO] Found USB device: $device_path"

modprobe option

new_id_path=""
for candidate in \
    "$SYS_ROOT/bus/usb-serial/drivers/option1/new_id" \
    "$SYS_ROOT/bus/usb-serial/drivers/option/new_id"; do
    if [[ -w "$candidate" ]]; then
        new_id_path="$candidate"
        break
    fi
done

if [[ -z "$new_id_path" ]]; then
    echo "[ERROR] Linux option driver new_id path was not found." >&2
    exit 3
fi

tmp_error="$(mktemp)"
if ! printf '%s %s\n' "$VID" "$PID" > "$new_id_path" 2>"$tmp_error"; then
    if ! grep -qi 'File exists' "$tmp_error"; then
        cat "$tmp_error" >&2
        rm -f "$tmp_error"
        exit 4
    fi
fi
rm -f "$tmp_error"

chmod a+rw "$DEV_ROOT"/ttyUSB* 2>/dev/null || true

echo "[INFO] Serial ports:"
if ! ls -l "$DEV_ROOT"/ttyUSB* 2>/dev/null; then
    echo "[WARN] No /dev/ttyUSB* ports appeared after binding ${VID}:${PID}."
    exit 5
fi

echo "[INFO] Detected AT serial port candidates:"
list_serial_ports
