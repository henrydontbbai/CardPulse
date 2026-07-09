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

expect_failure_contains "requires an argument" bash bin/cardpulse --config
expect_failure_contains "requires an argument" bash bin/cardpulse --notify-channel
expect_failure_contains "Unknown notification channel" bash bin/cardpulse --notify --notify-channel invalid
expect_failure_contains "--notify-channel requires --notify" bash bin/cardpulse --notify-channel telegram

help_output=$(bash bin/cardpulse --help 2>&1)
if [[ "$help_output" != *"--doctor"* ]]; then
    fail "CLI help missing --doctor option"
fi
if [[ "$help_output" != *"--sms-status"* || "$help_output" != *"--inbox"* || "$help_output" != *"--read-sms INDEX"* || "$help_output" != *"--delete-sms INDEX"* ]]; then
    fail "CLI help missing SMS inbox options"
fi
if [[ "$help_output" != *"will not send SMS or write CardPulse state"* ]]; then
    fail "CLI help must state doctor will not send SMS"
fi
grep -q -- '--doctor' README.md || fail "README CLI options missing --doctor"
grep -q 'Ubuntu ARM64' README.md || fail "README missing Apple Silicon Ubuntu ARM64 route"
grep -q 'linux_arm64' README.md || fail "README missing VoHive linux_arm64 guidance"
grep -q '未授权时发送真实短信' README.md || fail "README missing no-unauthorized-SMS warning"
grep -q 'scripts/vm-readonly-check.sh' docs/hardware-diagnostics.md || fail "hardware docs missing VM readonly script"
grep -q 'no SMS sent' scripts/vm-readonly-check.sh || fail "VM readonly check missing safety text"

doctor_config_dir=$(mktemp -d)
doctor_bin_dir=$(mktemp -d)
trap 'rm -rf "$doctor_config_dir" "$doctor_bin_dir"' EXIT

cat > "$doctor_config_dir/config.yaml" <<'YAML'
serial:
  port: "/tmp/cardpulse-doctor-missing-tty"
  baudrate: 115200
  auto_detect: false
sms:
  phone: "+8613800138000"
  message: "doctor check"
  interval_days: 179
  timeout: 30
retry:
  max_attempts: 1
  interval: 1
notify:
  enabled: false
logging:
  level: "INFO"
  file: "logs/cardpulse.log"
YAML

cat > "$doctor_bin_dir/yq" <<'SH'
#!/bin/sh
key="$2"
case "$key" in
  .serial.port) echo "/tmp/cardpulse-doctor-missing-tty" ;;
  .serial.baudrate) echo "115200" ;;
  .serial.auto_detect) echo "false" ;;
  *) echo "" ;;
esac
SH
chmod +x "$doctor_bin_dir/yq"

doctor_output=$(PATH="$doctor_bin_dir:$PATH" CARDPULSE_CONFIG_DIR="$doctor_config_dir" bash bin/cardpulse --doctor 2>&1 || true)
if [[ "$doctor_output" != *"CardPulse doctor"* ]]; then
    echo "$doctor_output" >&2
    fail "doctor output missing diagnostic header"
fi
if [[ "$doctor_output" != *"will not send SMS"* ]]; then
    echo "$doctor_output" >&2
    fail "doctor output must state it will not send SMS"
fi
if [[ "$doctor_output" != *"AT query commands"* ]]; then
    echo "$doctor_output" >&2
    fail "doctor output must disclose AT query commands"
fi
if [[ "$doctor_output" != *"No AT serial port found"* ]]; then
    echo "$doctor_output" >&2
    fail "doctor output missing no-serial explanation"
fi
if [[ -e "$doctor_config_dir/state" || -e "$doctor_config_dir/state/cardpulse.lock" || -e "$doctor_config_dir/.cardpulse.lock" ]]; then
    fail "doctor must not create state or lock files"
fi

missing_config_dir=$(mktemp -d)
missing_config_output=$(PATH="$doctor_bin_dir:$PATH" CARDPULSE_CONFIG_DIR="$missing_config_dir" bash bin/cardpulse --doctor 2>&1 || true)
if [[ "$missing_config_output" != *"Serial candidates:"* || "$missing_config_output" != *"Linux MBIM/QMI candidates:"* || "$missing_config_output" != *"USB hints:"* ]]; then
    echo "$missing_config_output" >&2
    fail "doctor should print hardware diagnostics even when config is missing"
fi
if [[ "$missing_config_output" != *"Config: unavailable"* ]]; then
    echo "$missing_config_output" >&2
    fail "doctor should report missing config separately"
fi

config_validate_output=$(
    source lib/config_reader.sh
    CONFIG_FILE="$doctor_config_dir/config.yaml"
    config_read() {
        case "$1" in
          .serial.port) echo "" ;;
          .serial.auto_detect) echo "True" ;;
          .serial.baudrate) echo "115200" ;;
          .sms.phone) echo "+8613800138000" ;;
          .sms.interval_days) echo "179" ;;
          .sms.timeout) echo "30" ;;
          .retry.max_attempts) echo "1" ;;
          .retry.interval) echo "1" ;;
          *) echo "${2:-}" ;;
        esac
    }
    config_validate 2>&1
)
if [[ "$config_validate_output" == *"serial.port"* ]]; then
    echo "$config_validate_output" >&2
    fail "config_validate should accept Python YAML boolean True for serial.auto_detect"
fi

notify_validate_output=$(
    source lib/config_reader.sh
    CONFIG_FILE="$doctor_config_dir/config.yaml"
    config_read() {
        case "$1" in
          .serial.port) echo "/tmp/cardpulse-doctor-missing-tty" ;;
          .serial.auto_detect) echo "False" ;;
          .serial.baudrate) echo "115200" ;;
          .sms.phone) echo "+8613800138000" ;;
          .sms.interval_days) echo "179" ;;
          .sms.timeout) echo "30" ;;
          .retry.max_attempts) echo "1" ;;
          .retry.interval) echo "1" ;;
          .notify.enabled) echo "True" ;;
          .notify.telegram.enabled) echo "True" ;;
          .notify.telegram.bot_token) echo "" ;;
          .notify.telegram.chat_id) echo "" ;;
          *) echo "${2:-}" ;;
        esac
    }
    config_validate 2>&1 || true
)
if [[ "$notify_validate_output" != *"telegram.bot_token"* || "$notify_validate_output" != *"telegram.chat_id"* ]]; then
    echo "$notify_validate_output" >&2
    fail "config_validate should treat notification boolean True as enabled"
fi

wwan_root=$(mktemp -d)
mkdir -p "$wwan_root/dev"
: > "$wwan_root/dev/cdc-wdm0"
: > "$wwan_root/dev/wwan0"
wwan_test_output=$(PATH="$doctor_bin_dir:$PATH" CARDPULSE_CONFIG_DIR="$doctor_config_dir" CARDPULSE_DEV_ROOT="$wwan_root/dev" CARDPULSE_SYS_ROOT="$wwan_root/sys" bash bin/cardpulse --doctor 2>&1 || true)
if [[ "$wwan_test_output" != *"Linux MBIM/QMI control candidate found"* ]]; then
    echo "$wwan_test_output" >&2
    fail "doctor should distinguish strong MBIM/QMI control candidates"
fi

vm_check_root=$(mktemp -d)
mkdir -p "$vm_check_root/dev" "$vm_check_root/sys/bus/usb/devices/1-1"
: > "$vm_check_root/dev/cdc-wdm0"
: > "$vm_check_root/dev/wwan0"
printf '2ca3\n' > "$vm_check_root/sys/bus/usb/devices/1-1/idVendor"
printf '4006\n' > "$vm_check_root/sys/bus/usb/devices/1-1/idProduct"
printf 'Baiwang\n' > "$vm_check_root/sys/bus/usb/devices/1-1/product"
vm_check_output=$(CARDPULSE_DEV_ROOT="$vm_check_root/dev" CARDPULSE_SYS_ROOT="$vm_check_root/sys" bash scripts/vm-readonly-check.sh 2>&1 || true)
if [[ "$vm_check_output" != *"MBIM_QMI_CONTROL_CANDIDATE=1"* || "$vm_check_output" != *"WWAN_NETWORK_CANDIDATE=1"* ]]; then
    echo "$vm_check_output" >&2
    fail "VM readonly check should report MBIM/QMI and WWAN candidates"
fi
if [[ "$vm_check_output" != *"vendor=2ca3 product=4006"* ]]; then
    echo "$vm_check_output" >&2
    fail "VM readonly check should report DJI/Baiwang sysfs hints"
fi

vm_empty_root=$(mktemp -d)
mkdir -p "$vm_empty_root/dev" "$vm_empty_root/sys/bus/usb/devices"
vm_empty_output=$(CARDPULSE_DEV_ROOT="$vm_empty_root/dev" CARDPULSE_SYS_ROOT="$vm_empty_root/sys" bash scripts/vm-readonly-check.sh 2>&1 || true)
if [[ "$vm_empty_output" != *"AT_SERIAL_CANDIDATE=0"* || "$vm_empty_output" != *"MBIM_QMI_CONTROL_CANDIDATE=0"* || "$vm_empty_output" != *"WWAN_NETWORK_CANDIDATE=0"* ]]; then
    echo "$vm_empty_output" >&2
    fail "VM readonly check should report no candidates in empty environment"
fi
if [[ "$vm_empty_output" != *"readonly check only; no SMS sent"* ]]; then
    echo "$vm_empty_output" >&2
    fail "VM readonly check should print safety text"
fi

auto_detect_output=$(
    source lib/config_reader.sh
    source lib/sms_sender.sh
    config_read() {
        if [[ "$1" == ".serial.auto_detect" ]]; then
            echo "True"
        else
            echo ""
        fi
    }
    at_detect_device() {
        echo "/tmp/cardpulse-fake-serial"
    }
    sms_detect_device
)
if [[ "$auto_detect_output" != "/tmp/cardpulse-fake-serial" ]]; then
    fail "sms_detect_device should accept Python YAML boolean True"
fi

auto_detect_probe_output=$(
    source lib/config_reader.sh
    source lib/at_modem.sh
    source lib/sms_sender.sh
    config_read() {
        case "$1" in
          .serial.port) echo "" ;;
          .serial.auto_detect) echo "true" ;;
          *) echo "${2:-}" ;;
        esac
    }
    at_list_candidate_devices() {
        printf '%s\n' "/tmp/cardpulse-tty-miss" "/tmp/cardpulse-tty-hit"
    }
    at_probe_device() {
        [[ "$1" == "/tmp/cardpulse-tty-hit" ]]
    }
    sms_detect_device
)
if [[ "$auto_detect_probe_output" != "/tmp/cardpulse-tty-hit" ]]; then
    fail "sms_detect_device should prefer a candidate that answers AT"
fi

auto_detect_no_probe_output=$(
    source lib/config_reader.sh
    source lib/at_modem.sh
    source lib/sms_sender.sh
    config_read() {
        case "$1" in
          .serial.port) echo "" ;;
          .serial.auto_detect) echo "true" ;;
          *) echo "${2:-}" ;;
        esac
    }
    at_list_candidate_devices() {
        printf '%s\n' "/tmp/cardpulse-tty-miss-a" "/tmp/cardpulse-tty-miss-b"
    }
    at_probe_device() {
        return 1
    }
    sms_detect_device || true
)
if [[ -n "$auto_detect_no_probe_output" ]]; then
    echo "$auto_detect_no_probe_output" >&2
    fail "sms_detect_device should fail when candidates exist but none answer AT"
fi

configured_port_fallback_root=$(mktemp -d)
touch "$configured_port_fallback_root/configured" "$configured_port_fallback_root/detected"
configured_port_fallback_output=$(
    source lib/config_reader.sh
    source lib/at_modem.sh
    source lib/sms_sender.sh
    config_read() {
        case "$1" in
          .serial.port) echo "$configured_port_fallback_root/configured" ;;
          .serial.auto_detect) echo "true" ;;
          *) echo "${2:-}" ;;
        esac
    }
    at_probe_device() {
        [[ "$1" == "$configured_port_fallback_root/detected" ]]
    }
    at_list_candidate_devices() {
        printf '%s\n' "$configured_port_fallback_root/configured" "$configured_port_fallback_root/detected"
    }
    sms_detect_device
)
if [[ "$configured_port_fallback_output" != "$configured_port_fallback_root/detected" ]]; then
    echo "$configured_port_fallback_output" >&2
    fail "sms_detect_device should fall back from an unresponsive configured port when auto_detect=true"
fi

configured_port_strict_output=$(
    source lib/config_reader.sh
    source lib/at_modem.sh
    source lib/sms_sender.sh
    config_read() {
        case "$1" in
          .serial.port) echo "$configured_port_fallback_root/configured" ;;
          .serial.auto_detect) echo "false" ;;
          *) echo "${2:-}" ;;
        esac
    }
    at_probe_device() {
        return 1
    }
    sms_detect_device || true
)
if [[ -n "$configured_port_strict_output" ]]; then
    echo "$configured_port_strict_output" >&2
    fail "sms_detect_device should not silently use an unresponsive configured port when auto_detect=false"
fi

status_root=$(mktemp -d)
mkdir -p "$status_root/state"
printf '%s\n' "$(( $(date +%s) - 86400 ))" > "$status_root/state/last_success"
printf '2026-07-09 11:02:03\n' > "$status_root/state/last_success_date"
printf '%s|2026-07-09 11:02:03|success\n' "$(date +%s)" > "$status_root/state/history.log"
status_output=$(
    source lib/config_reader.sh
    source lib/state_manager.sh
    CONFIG_DIR="$status_root"
    CONFIG_FILE="$status_root/config.yaml"
    STATE_DIR="$status_root/state"
    config_read() {
        case "$1" in
          .sms.interval_days) echo "179" ;;
          *) echo "${2:-}" ;;
        esac
    }
    state_show_status
)
if [[ "$status_output" != *"Last send:"* || "$status_output" != *"Send due: no"* || "$status_output" != *"Next send:"* || "$status_output" != *"Last result: success"* ]]; then
    echo "$status_output" >&2
    fail "status output should expose stable schedule summary fields"
fi

custom_state_root=$(mktemp -d)
mkdir -p "$custom_state_root/custom-state"
printf '1234567890\n' > "$custom_state_root/custom-state/last_success"
custom_state_output=$(
    export CARDPULSE_STATE_DIR="$custom_state_root/custom-state"
    source lib/state_manager.sh
    state_get_last_success
)
if [[ "$custom_state_output" != "1234567890" ]]; then
    echo "$custom_state_output" >&2
    fail "state manager should respect CARDPULSE_STATE_DIR override"
fi

sms_status_output=$(
    source lib/sms_receiver.sh
    at_send() {
        case "$1" in
          "AT+CSMS?") printf '\r\n+CSMS: 0,1,1,1\r\n\r\nOK\r\n' ;;
          "AT+CPMS?") printf '\r\n+CPMS: "ME",23,23,"ME",23,23,"ME",23,23\r\n\r\nOK\r\n' ;;
          "AT+CMGF?") printf '\r\n+CMGF: 0\r\n\r\nOK\r\n' ;;
          "AT+CNMI?") printf '\r\n+CNMI: 2,1,0,0,0\r\n\r\nOK\r\n' ;;
          *) printf '\r\nERROR\r\n' ;;
        esac
    }
    sms_receive_status
)
if [[ "$sms_status_output" != *"Storage: ME 23/23 FULL"* || "$sms_status_output" != *"Format: PDU"* || "$sms_status_output" != *"New message indication: 2,1,0,0,0"* ]]; then
    echo "$sms_status_output" >&2
    fail "sms status should summarize full storage, PDU mode, and CNMI"
fi

sms_inbox_output=$(
    source lib/sms_receiver.sh
    at_send() {
        case "$1" in
          "AT+CMGF=0") printf '\r\nOK\r\n' ;;
          "AT+CMGL=4") printf '\r\n+CMGL: 1,1,,25\r\n00040D91683120553293F100086270807164000004004F004B\r\n\r\nOK\r\n' ;;
          *) printf '\r\nERROR\r\n' ;;
        esac
    }
    sms_receive_list
)
if [[ "$sms_inbox_output" != *"Index: 1"* || "$sms_inbox_output" != *"Status: REC READ"* || "$sms_inbox_output" != *"From: +8613025523391"* || "$sms_inbox_output" != *"Preview: OK"* ]]; then
    echo "$sms_inbox_output" >&2
    fail "sms inbox should decode common PDU SMS-DELIVER entries"
fi

sms_read_output=$(
    source lib/sms_receiver.sh
    at_send() {
        case "$1" in
          "AT+CMGF=0") printf '\r\nOK\r\n' ;;
          "AT+CMGR=1") printf '\r\n+CMGR: 1,,25\r\n00040D91683120553293F100086270807164000004004F004B\r\n\r\nOK\r\n' ;;
          *) printf '\r\nERROR\r\n' ;;
        esac
    }
    sms_receive_read 1
)
if [[ "$sms_read_output" != *"Index: 1"* || "$sms_read_output" != *"Message: OK"* ]]; then
    echo "$sms_read_output" >&2
    fail "sms read should decode one PDU message"
fi

concat_pdus=$(
python3 - <<'PY'
import math

GSM_7BIT = (
    "@", "\u00a3", "$", "\u00a5", "\u00e8", "\u00e9", "\u00f9", "\u00ec",
    "\u00f2", "\u00c7", "\n", "\u00d8", "\u00f8", "\r", "\u00c5", "\u00e5",
    "\u0394", "_", "\u03a6", "\u0393", "\u039b", "\u03a9", "\u03a0", "\u03a8",
    "\u03a3", "\u0398", "\u039e", None, "\u00c6", "\u00e6", "\u00df", "\u00c9",
    " ", "!", '"', "#", "\u00a4", "%", "&", "'",
    "(", ")", "*", "+", ",", "-", ".", "/",
    "0", "1", "2", "3", "4", "5", "6", "7",
    "8", "9", ":", ";", "<", "=", ">", "?",
    "\u00a1", "A", "B", "C", "D", "E", "F", "G",
    "H", "I", "J", "K", "L", "M", "N", "O",
    "P", "Q", "R", "S", "T", "U", "V", "W",
    "X", "Y", "Z", "\u00c4", "\u00d6", "\u00d1", "\u00dc", "\u00a7",
    "\u00bf", "a", "b", "c", "d", "e", "f", "g",
    "h", "i", "j", "k", "l", "m", "n", "o",
    "p", "q", "r", "s", "t", "u", "v", "w",
    "x", "y", "z", "\u00e4", "\u00f6", "\u00f1", "\u00fc", "\u00e0",
)
GSM_EXT = {"^": 0x14, "{": 0x28, "}": 0x29, "\\": 0x2F, "[": 0x3C, "~": 0x3D, "]": 0x3E, "|": 0x40, "\u20ac": 0x65}
CHAR_TO_GSM = {}
for idx, ch in enumerate(GSM_7BIT):
    if ch is not None:
        CHAR_TO_GSM[ch] = (idx, False)
for ch, idx in GSM_EXT.items():
    CHAR_TO_GSM[ch] = (idx, True)

def swap_digits(value):
    if len(value) % 2:
        value += "F"
    return "".join(value[i + 1] + value[i] for i in range(0, len(value), 2))

def gsm7_septets(text):
    result = []
    for ch in text:
        idx, is_ext = CHAR_TO_GSM[ch]
        if is_ext:
            result.append(0x1B)
        result.append(idx)
    return result

def pack_septets(septets, skip_bits=0, prefix_bytes=b""):
    total_bits = skip_bits + len(septets) * 7
    out = bytearray(max(len(prefix_bytes), math.ceil(total_bits / 8)))
    for i, octet in enumerate(prefix_bytes):
        out[i] = octet
    bit_pos = skip_bits
    for septet in septets:
        for bit_index in range(7):
            if septet & (1 << bit_index):
                absolute = bit_pos + bit_index
                out[absolute // 8] |= 1 << (absolute % 8)
        bit_pos += 7
    return bytes(out)

def build_part(text, seq):
    sender = "12345678901"
    udh = bytes([0x05, 0x00, 0x03, 0x07, 0x02, seq])
    septets = gsm7_septets(text)
    header_septets = math.ceil(len(udh) * 8 / 7)
    payload = pack_septets(septets, skip_bits=header_septets * 7, prefix_bytes=udh)
    udl = header_septets + len(septets)
    print(
        "00"
        "44"
        f"{len(sender):02X}"
        "91"
        f"{swap_digits(sender)}"
        "00"
        "00"
        "62708021436500"
        f"{udl:02X}"
        f"{payload.hex().upper()}"
    )

build_part("Hello ", 1)
build_part("world", 2)
PY
)
concat_pdu_1=$(printf '%s\n' "$concat_pdus" | sed -n '1p')
concat_pdu_2=$(printf '%s\n' "$concat_pdus" | sed -n '2p')
sms_concat_output=$(
    source lib/sms_receiver.sh
    at_send() {
        case "$1" in
          "AT+CMGF=0") printf '\r\nOK\r\n' ;;
          "AT+CMGL=4") printf '\r\n+CMGL: 1,0,,32\r\n%s\r\n+CMGL: 2,0,,31\r\n%s\r\n\r\nOK\r\n' "$concat_pdu_1" "$concat_pdu_2" ;;
          *) printf '\r\nERROR\r\n' ;;
        esac
    }
    sms_receive_list
)
if [[ "$sms_concat_output" != *"Indexes: 1,2"* || "$sms_concat_output" != *"Parts: 2/2"* || "$sms_concat_output" != *"Preview: Hello world"* ]]; then
    echo "$sms_concat_output" >&2
    fail "sms inbox should merge concatenated PDU parts within one listing"
fi
if [[ "$sms_concat_output" != *"Status: REC UNREAD"* ]]; then
    echo "$sms_concat_output" >&2
    fail "sms inbox should keep grouped status labels human-readable"
fi

concat_pdus_different_time=$(
python3 - <<'PY'
import math

GSM_7BIT = (
    "@", "\u00a3", "$", "\u00a5", "\u00e8", "\u00e9", "\u00f9", "\u00ec",
    "\u00f2", "\u00c7", "\n", "\u00d8", "\u00f8", "\r", "\u00c5", "\u00e5",
    "\u0394", "_", "\u03a6", "\u0393", "\u039b", "\u03a9", "\u03a0", "\u03a8",
    "\u03a3", "\u0398", "\u039e", None, "\u00c6", "\u00e6", "\u00df", "\u00c9",
    " ", "!", '"', "#", "\u00a4", "%", "&", "'",
    "(", ")", "*", "+", ",", "-", ".", "/",
    "0", "1", "2", "3", "4", "5", "6", "7",
    "8", "9", ":", ";", "<", "=", ">", "?",
    "\u00a1", "A", "B", "C", "D", "E", "F", "G",
    "H", "I", "J", "K", "L", "M", "N", "O",
    "P", "Q", "R", "S", "T", "U", "V", "W",
    "X", "Y", "Z", "\u00c4", "\u00d6", "\u00d1", "\u00dc", "\u00a7",
    "\u00bf", "a", "b", "c", "d", "e", "f", "g",
    "h", "i", "j", "k", "l", "m", "n", "o",
    "p", "q", "r", "s", "t", "u", "v", "w",
    "x", "y", "z", "\u00e4", "\u00f6", "\u00f1", "\u00fc", "\u00e0",
)
CHAR_TO_GSM = {ch: (idx, False) for idx, ch in enumerate(GSM_7BIT) if ch is not None}

def swap_digits(value):
    if len(value) % 2:
        value += "F"
    return "".join(value[i + 1] + value[i] for i in range(0, len(value), 2))

def gsm7_septets(text):
    return [CHAR_TO_GSM[ch][0] for ch in text]

def pack_septets(septets, skip_bits=0, prefix_bytes=b""):
    total_bits = skip_bits + len(septets) * 7
    out = bytearray(max(len(prefix_bytes), math.ceil(total_bits / 8)))
    for i, octet in enumerate(prefix_bytes):
        out[i] = octet
    bit_pos = skip_bits
    for septet in septets:
        for bit_index in range(7):
            if septet & (1 << bit_index):
                absolute = bit_pos + bit_index
                out[absolute // 8] |= 1 << (absolute % 8)
        bit_pos += 7
    return bytes(out)

def build_part(text, seq, timestamp):
    sender = "12345678901"
    udh = bytes([0x05, 0x00, 0x03, 0x07, 0x02, seq])
    septets = gsm7_septets(text)
    header_septets = math.ceil(len(udh) * 8 / 7)
    payload = pack_septets(septets, skip_bits=header_septets * 7, prefix_bytes=udh)
    udl = header_septets + len(septets)
    print(
        "00"
        "44"
        f"{len(sender):02X}"
        "91"
        f"{swap_digits(sender)}"
        "00"
        "00"
        f"{timestamp}"
        f"{udl:02X}"
        f"{payload.hex().upper()}"
    )

build_part("Hello ", 1, "62708021436500")
build_part("world", 2, "62708021437500")
PY
)
concat_time_pdu_1=$(printf '%s\n' "$concat_pdus_different_time" | sed -n '1p')
concat_time_pdu_2=$(printf '%s\n' "$concat_pdus_different_time" | sed -n '2p')
sms_concat_time_output=$(
    source lib/sms_receiver.sh
    at_send() {
        case "$1" in
          "AT+CMGF=0") printf '\r\nOK\r\n' ;;
          "AT+CMGL=4") printf '\r\n+CMGL: 3,0,,32\r\n%s\r\n+CMGL: 4,0,,31\r\n%s\r\n\r\nOK\r\n' "$concat_time_pdu_1" "$concat_time_pdu_2" ;;
          *) printf '\r\nERROR\r\n' ;;
        esac
    }
    sms_receive_list
)
if [[ "$sms_concat_time_output" != *"Indexes: 3,4"* || "$sms_concat_time_output" != *"Preview: Hello world"* ]]; then
    echo "$sms_concat_time_output" >&2
    fail "sms inbox should merge concatenated SMS parts even if modem timestamps differ"
fi

delete_missing_confirm_output=$(bash bin/cardpulse --delete-sms 1 2>&1 || true)
if [[ "$delete_missing_confirm_output" != *"--confirm DELETE_SMS"* ]]; then
    echo "$delete_missing_confirm_output" >&2
    fail "delete-sms must require confirmation before config or device access"
fi

expect_failure_contains "SMS index must be a single non-negative integer" bash bin/cardpulse --read-sms 1-3
expect_failure_contains "SMS index must be a single non-negative integer" bash bin/cardpulse --delete-sms abc --confirm DELETE_SMS

grep -q 'acquire_singleton_lock' bin/cardpulse || fail "CLI missing singleton lock helper"
grep -q 'flock not found' bin/cardpulse || fail "CLI missing macOS flock fallback warning"
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

if grep -R 'match($0' lib bin scripts >/dev/null; then
    fail "found gawk-only awk match capture array pattern"
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
grep -q 'pdu_decoder.py' scripts/install.sh || fail "installer should install PDU decoder"
grep -q 'pdu_decoder.py' Dockerfile || fail "Docker image should include PDU decoder"

grep -q 'at_configure_stty' lib/at_modem.sh || fail "AT modem missing portable stty helper"
grep -q 'gtimeout' lib/at_modem.sh || fail "AT modem missing macOS gtimeout support"
grep -q '/dev/cu.usbserial\*' lib/at_modem.sh || fail "AT modem missing macOS USB serial detection"

stty_fallback_dir=$(mktemp -d)
stty_fallback_log="$stty_fallback_dir/stty.log"
cat > "$stty_fallback_dir/stty" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STTY_FALLBACK_LOG"
if [[ "$*" == "-F $STTY_FALLBACK_PORT 115200 raw" ]]; then
    exit 0
fi
exit 1
SH
chmod +x "$stty_fallback_dir/stty"
STTY_FALLBACK_PORT="$stty_fallback_dir/fake-tty" \
STTY_FALLBACK_LOG="$stty_fallback_log" \
PATH="$stty_fallback_dir:$PATH" \
bash -c 'source lib/at_modem.sh; at_configure_stty "$STTY_FALLBACK_PORT" 115200' || fail "AT modem should fall back to minimal stty raw mode"
grep -q -- "-F $stty_fallback_dir/fake-tty 115200 raw -echo -echoe -echok" "$stty_fallback_log" || fail "AT modem did not try full Linux stty first"
grep -q -- "-F $stty_fallback_dir/fake-tty 115200 raw$" "$stty_fallback_log" || fail "AT modem did not try minimal Linux stty fallback"

stty_skip_dir=$(mktemp -d)
cat > "$stty_skip_dir/stty" <<'SH'
#!/usr/bin/env bash
exit 1
SH
chmod +x "$stty_skip_dir/stty"
touch "$stty_skip_dir/fake-tty"
stty_skip_output=$(
    STTY_SKIP_PORT="$stty_skip_dir/fake-tty" \
    PATH="$stty_skip_dir:$PATH" \
    bash -c 'source lib/at_modem.sh; at_init "$STTY_SKIP_PORT" 115200; rc=$?; at_close; exit "$rc"' 2>&1
) || fail "AT modem should continue when stty cannot configure a still-openable port"
if [[ "$stty_skip_output" != *"stty failed"* ]]; then
    echo "$stty_skip_output" >&2
    fail "AT modem should warn when continuing after stty failure"
fi

parsed_info_value=$(printf '\r\nBaiwang\r\n\r\nOK\r\n' | bash -c 'source lib/at_modem.sh; at_first_response_value')
if [[ "$parsed_info_value" != "Baiwang" ]]; then
    fail "AT modem should ignore leading blank lines when parsing info responses"
fi

echo "shell behavior tests ok"
