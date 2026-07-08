#!/bin/bash
# CardPulse - SMS receive/inbox helpers.

[[ -n "${_SMS_RECEIVER_LOADED:-}" ]] && return 0
_SMS_RECEIVER_LOADED=1

sms_receive_validate_index() {
    local index="${1:-}"
    if [[ ! "$index" =~ ^[0-9]+$ ]]; then
        echo "SMS index must be a single non-negative integer" >&2
        return 1
    fi
    return 0
}

sms_receive_status_label() {
    case "${1:-}" in
        0) echo "REC UNREAD" ;;
        1) echo "REC READ" ;;
        2) echo "STO UNSENT" ;;
        3) echo "STO SENT" ;;
        4) echo "ALL" ;;
        *) echo "UNKNOWN" ;;
    esac
}

sms_receive_decoder_path() {
    local decoder=""
    if [[ -n "${CARDPULSE_LIB_DIR:-}" && -f "${CARDPULSE_LIB_DIR}/pdu_decoder.py" ]]; then
        decoder="${CARDPULSE_LIB_DIR}/pdu_decoder.py"
    else
        decoder="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pdu_decoder.py"
    fi
    if [[ ! -f "$decoder" ]]; then
        return 1
    fi
    echo "$decoder"
}

sms_receive_decode_pdu() {
    local pdu="$1"
    local decoder
    if ! decoder=$(sms_receive_decoder_path); then
        printf '{"ok": false, "error": "PDU decoder not found", "raw_pdu": "%s"}\n' "$pdu"
        return 0
    fi
    python3 "$decoder" "$pdu"
}

sms_receive_json_field() {
    local json="$1"
    local field="$2"
    printf '%s' "$json" | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data.get(sys.argv[1], ""))' "$field"
}

sms_receive_expect_ok() {
    local cmd="$1"
    local timeout="${2:-5}"
    local response
    response=$(at_send "$cmd" "$timeout")
    if echo "$response" | grep -q "OK"; then
        return 0
    fi
    echo "[ERROR] AT command failed: $cmd" >&2
    echo "$response" >&2
    return 1
}

sms_receive_storage_summary() {
    local response="$1"
    local cpms_re='^\+CPMS: "([^"]+)",([0-9]+),([0-9]+)'
    local line storage used total status
    while IFS= read -r line; do
        line="${line//$'\r'/}"
        if [[ "$line" =~ $cpms_re ]]; then
            storage="${BASH_REMATCH[1]}"
            used="${BASH_REMATCH[2]}"
            total="${BASH_REMATCH[3]}"
            status=""
            if (( used >= total && total > 0 )); then
                status=" FULL"
            fi
            echo "Storage: $storage $used/$total$status"
            return 0
        fi
    done <<< "$response"
}

sms_receive_status() {
    local csms cpms cmgf cnmi format cnmi_value
    csms=$(at_send "AT+CSMS?" 5)
    cpms=$(at_send "AT+CPMS?" 5)
    cmgf=$(at_send "AT+CMGF?" 5)
    cnmi=$(at_send "AT+CNMI?" 5)

    echo "=== SMS status ==="
    echo "$csms" | LC_ALL=C tr -d '\r' | grep '+CSMS:' | sed 's/^/Service: /' || echo "Service: UNKNOWN"
    sms_receive_storage_summary "$cpms"
    format=$(echo "$cmgf" | LC_ALL=C tr -d '\r' | awk -F': ' '/\+CMGF:/ {print $2; exit}')
    case "$format" in
        0) echo "Format: PDU" ;;
        1) echo "Format: TEXT" ;;
        *) echo "Format: UNKNOWN" ;;
    esac
    cnmi_value=$(echo "$cnmi" | LC_ALL=C tr -d '\r' | awk -F': ' '/\+CNMI:/ {print $2; exit}')
    echo "New message indication: ${cnmi_value:-UNKNOWN}"
}

sms_receive_print_entry() {
    local index="$1"
    local status="$2"
    local pdu="$3"
    local decoded sender timestamp text ok preview
    decoded=$(sms_receive_decode_pdu "$pdu")
    ok=$(sms_receive_json_field "$decoded" ok)
    sender=$(sms_receive_json_field "$decoded" sender)
    timestamp=$(sms_receive_json_field "$decoded" timestamp)
    text=$(sms_receive_json_field "$decoded" text)
    if [[ "$ok" != "True" && "$ok" != "true" ]]; then
        text="(decode failed: $(sms_receive_json_field "$decoded" error))"
    fi
    preview="$text"
    if [[ ${#preview} -gt 80 ]]; then
        preview="${preview:0:80}..."
    fi
    echo "Index: $index"
    echo "Status: $(sms_receive_status_label "$status")"
    echo "From: ${sender:-UNKNOWN}"
    echo "Time: ${timestamp:-UNKNOWN}"
    echo "Preview: $preview"
    if [[ "$ok" != "True" && "$ok" != "true" ]]; then
        echo "Raw PDU: $pdu"
    fi
    echo ""
}

sms_receive_list() {
    sms_receive_expect_ok "AT+CMGF=0" 5 || return 1
    local response
    response=$(at_send "AT+CMGL=4" 15)
    echo "=== SMS inbox ==="
    local index="" status="" found=false line
    while IFS= read -r line; do
        line="${line//$'\r'/}"
        if [[ "$line" =~ ^\+CMGL:\ ([0-9]+),([0-9]+) ]]; then
            index="${BASH_REMATCH[1]}"
            status="${BASH_REMATCH[2]}"
            continue
        fi
        if [[ -n "$index" && "$line" =~ ^[0-9A-Fa-f]+$ ]]; then
            sms_receive_print_entry "$index" "$status" "$line"
            found=true
            index=""
            status=""
        fi
    done <<< "$response"
    if [[ "$found" != "true" ]]; then
        echo "No SMS messages found."
    fi
}

sms_receive_read() {
    local index="$1"
    sms_receive_validate_index "$index" || return 1
    sms_receive_expect_ok "AT+CMGF=0" 5 || return 1
    local response
    response=$(at_send "AT+CMGR=$index" 10)
    local status="" pdu="" line
    while IFS= read -r line; do
        line="${line//$'\r'/}"
        if [[ "$line" =~ ^\+CMGR:\ ([0-9]+) ]]; then
            status="${BASH_REMATCH[1]}"
            continue
        fi
        if [[ "$line" =~ ^[0-9A-Fa-f]+$ ]]; then
            pdu="$line"
            break
        fi
    done <<< "$response"
    if [[ -z "$pdu" ]]; then
        echo "[ERROR] SMS index not found or message has no PDU: $index" >&2
        echo "$response" >&2
        return 1
    fi

    local decoded ok sender timestamp text
    decoded=$(sms_receive_decode_pdu "$pdu")
    ok=$(sms_receive_json_field "$decoded" ok)
    sender=$(sms_receive_json_field "$decoded" sender)
    timestamp=$(sms_receive_json_field "$decoded" timestamp)
    text=$(sms_receive_json_field "$decoded" text)

    echo "=== SMS message ==="
    echo "Index: $index"
    echo "Status: $(sms_receive_status_label "$status")"
    echo "From: ${sender:-UNKNOWN}"
    echo "Time: ${timestamp:-UNKNOWN}"
    if [[ "$ok" == "True" || "$ok" == "true" ]]; then
        echo "Message: $text"
    else
        echo "Message: (decode failed: $(sms_receive_json_field "$decoded" error))"
        echo "Raw PDU: $pdu"
    fi
}

sms_receive_delete() {
    local index="$1"
    sms_receive_validate_index "$index" || return 1
    local response
    response=$(at_send "AT+CMGD=$index" 10)
    if echo "$response" | grep -q "OK"; then
        echo "Deleted SMS index: $index"
        return 0
    fi
    echo "[ERROR] Failed to delete SMS index: $index" >&2
    echo "$response" >&2
    return 1
}
