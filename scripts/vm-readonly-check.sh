#!/usr/bin/env bash
set -u

DEV_ROOT="${CARDPULSE_DEV_ROOT:-/dev}"
SYS_ROOT="${CARDPULSE_SYS_ROOT:-/sys}"

any_path_matches() {
    local pattern
    local match

    for pattern in "$@"; do
        for match in $pattern; do
            [[ -e "$match" ]] && return 0
        done
    done

    return 1
}

echo "== CardPulse DJI VM readonly check =="
date -Is 2>/dev/null || date

echo
echo "== machine =="
uname -a || true
uname -m || true
cat /etc/os-release 2>/dev/null || true

echo
echo "== usb devices =="
if command -v lsusb >/dev/null 2>&1; then
    lsusb || true
    echo
    echo "== DJI/Baiwang/known modem hints =="
    lsusb | grep -Ei '2ca3|4006|DJI|Baiwang|Quectel|Huawei|SIMCom|EC20|EC25|ME909|SIM7600' || true
else
    echo "lsusb missing; install usbutils only if explicitly approved"
fi

echo
echo "== device nodes =="
ls -l "$DEV_ROOT"/cdc-wdm* "$DEV_ROOT"/wwan* "$DEV_ROOT"/ttyUSB* "$DEV_ROOT"/ttyACM* 2>/dev/null || true

echo
echo "== network links =="
ip -brief link 2>/dev/null | grep -Ei 'wwan|usb|enx|cdc|qmi|mbim' || true

echo
echo "== kernel drivers/modules =="
lsmod 2>/dev/null | grep -Ei 'qmi_wwan|cdc_mbim|wwan|cdc_wdm|usbserial|option' || true

echo
echo "== usb driver bindings =="
for driver in cdc_mbim qmi_wwan wwan_qmi cdc_wdm option usbserial; do
    if [[ -d "$SYS_ROOT/bus/usb/drivers/$driver" ]]; then
        echo "-- $driver --"
        find "$SYS_ROOT/bus/usb/drivers/$driver" -maxdepth 1 -type l -printf '%f -> %l\n' 2>/dev/null || true
    fi
done

echo
echo "== sysfs vendor/product hints =="
find "$SYS_ROOT/bus/usb/devices" -maxdepth 2 -name idVendor -print 2>/dev/null | while read -r vendor_file; do
    device_dir=$(dirname "$vendor_file")
    vendor=$(cat "$device_dir/idVendor" 2>/dev/null || true)
    product=$(cat "$device_dir/idProduct" 2>/dev/null || true)
    product_name=$(cat "$device_dir/product" 2>/dev/null || true)
    manufacturer=$(cat "$device_dir/manufacturer" 2>/dev/null || true)
    case "${vendor}:${product} ${product_name} ${manufacturer}" in
        *2ca3:4006*|*2CA3:4006*|*DJI*|*Baiwang*|*Quectel*|*Huawei*|*SIMCom*)
            echo "$device_dir vendor=$vendor product=$product manufacturer=$manufacturer name=$product_name"
            ;;
    esac
done

echo
echo "== decision hints =="
if any_path_matches "$DEV_ROOT"/ttyUSB* "$DEV_ROOT"/ttyACM*; then
    echo "AT_SERIAL_CANDIDATE=1"
else
    echo "AT_SERIAL_CANDIDATE=0"
fi

if any_path_matches "$DEV_ROOT"/cdc-wdm* "$DEV_ROOT"/wwan*qmi* "$DEV_ROOT"/wwan*mbim*; then
    echo "MBIM_QMI_CONTROL_CANDIDATE=1"
else
    echo "MBIM_QMI_CONTROL_CANDIDATE=0"
fi

if any_path_matches "$DEV_ROOT"/wwan* || ip -brief link 2>/dev/null | grep -qi wwan; then
    echo "WWAN_NETWORK_CANDIDATE=1"
else
    echo "WWAN_NETWORK_CANDIDATE=0"
fi

echo
echo "== safety =="
echo "readonly check only; no SMS sent; no driver install; no system config changed"
