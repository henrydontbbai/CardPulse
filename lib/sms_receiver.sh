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

sms_receive_json_array_field() {
    local json="$1"
    local field="$2"
    printf '%s' "$json" | python3 -c '
import json, sys
data = json.load(sys.stdin)
value = data.get(sys.argv[1], [])
if isinstance(value, list):
    print(",".join(str(item) for item in value))
else:
    print(value if value is not None else "")
' "$field"
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

sms_receive_flush_concat_groups() {
    local groups_json="$1"
    if [[ -z "$groups_json" || "$groups_json" == "{}" ]]; then
        return 0
    fi

    GROUPS_JSON="$groups_json" python3 - <<'PY'
import json
import os

groups = json.loads(os.environ["GROUPS_JSON"])
def group_sort_key(item):
    key, group = item
    indexes = [int(part["index"]) for part in group["parts"] if str(part.get("index", "")).isdigit()]
    return (group.get("timestamp", "UNKNOWN"), min(indexes) if indexes else 0, key)

for key, group in sorted(groups.items(), key=group_sort_key):
    group = groups[key]
    parts = sorted(group["parts"], key=lambda item: (item["seq"], int(item["index"])))
    preview = "".join(part["text"] for part in parts)
    sequences = {part["seq"] for part in parts}
    complete = len(parts) == group["total"] and sequences == set(range(1, group["total"] + 1))
    print(f"Indexes: {','.join(part['index'] for part in parts)}")
    status = group["status"]
    label = {
        "0": "REC UNREAD",
        "1": "REC READ",
        "2": "STO UNSENT",
        "3": "STO SENT",
        "4": "ALL",
    }.get(str(status), "UNKNOWN")
    print(f"Status: {label}")
    print(f"From: {group['sender']}")
    print(f"Time: {group['timestamp']}")
    print(f"Parts: {len(parts)}/{group['total']}")
    print(f"Complete: {'yes' if complete else 'no'}")
    print(f"Preview: {preview}")
    print()
PY
}

sms_receive_list() {
    sms_receive_expect_ok "AT+CMGF=0" 5 || return 1
    local response
    response=$(at_send "AT+CMGL=4" 15)
    echo "=== SMS inbox ==="
    local index="" status="" found=false line
    local concat_groups='{}'
    while IFS= read -r line; do
        line="${line//$'\r'/}"
        if [[ "$line" =~ ^\+CMGL:\ ([0-9]+),([0-9]+) ]]; then
            index="${BASH_REMATCH[1]}"
            status="${BASH_REMATCH[2]}"
            continue
        fi
        if [[ -n "$index" && "$line" =~ ^[0-9A-Fa-f]+$ ]]; then
            local decoded
            local ok
            local concat_ref
            local concat_total
            local concat_seq
            decoded=$(sms_receive_decode_pdu "$line")
            ok=$(sms_receive_json_field "$decoded" ok)
            concat_ref=$(sms_receive_json_field "$decoded" concat_ref)
            concat_total=$(sms_receive_json_field "$decoded" concat_total)
            concat_seq=$(sms_receive_json_field "$decoded" concat_seq)

            if [[ ( "$ok" == "True" || "$ok" == "true" ) && -n "$concat_ref" && -n "$concat_total" && -n "$concat_seq" ]]; then
                concat_groups=$(GROUPS_JSON="$concat_groups" ENTRY_JSON="$decoded" ENTRY_INDEX="$index" ENTRY_STATUS="$status" python3 - <<'PY'
import json
import os
from datetime import datetime

groups = json.loads(os.environ["GROUPS_JSON"])
entry = json.loads(os.environ["ENTRY_JSON"])
sender = entry.get("sender", "UNKNOWN")
concat_ref = str(entry.get("concat_ref", ""))
total = int(entry.get("concat_total") or 0)
seq = int(entry.get("concat_seq") or 0)
current_timestamp = entry.get("timestamp", "UNKNOWN")

def parse_timestamp(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None

def group_distance_seconds(group):
    current = parse_timestamp(current_timestamp)
    group_time = parse_timestamp(group.get("timestamp", ""))
    if current is None or group_time is None:
        return None
    return abs((current - group_time).total_seconds())

candidates = []
for key, candidate in groups.items():
    if (
        candidate.get("sender") != sender
        or candidate.get("concat_ref") != concat_ref
        or int(candidate.get("total") or 0) != total
    ):
        continue
    if seq in {int(part.get("seq") or 0) for part in candidate.get("parts", [])}:
        continue
    distance = group_distance_seconds(candidate)
    if distance is not None and distance <= 600:
        candidates.append((distance, key))

if candidates:
    _, group_key = min(candidates)
    group = groups[group_key]
else:
    base_key = f"{sender}|{concat_ref}|{total}"
    ordinal = 1
    group_key = f"{base_key}|{ordinal}"
    while group_key in groups:
        ordinal += 1
        group_key = f"{base_key}|{ordinal}"
    group = {
        "sender": sender,
        "concat_ref": concat_ref,
        "timestamp": current_timestamp,
        "status": os.environ["ENTRY_STATUS"],
        "total": total,
        "parts": [],
    }
    groups[group_key] = group

if group["timestamp"] == "UNKNOWN" or (
    current_timestamp != "UNKNOWN" and current_timestamp < group["timestamp"]
):
    group["timestamp"] = current_timestamp
group["parts"].append({
    "index": os.environ["ENTRY_INDEX"],
    "seq": seq,
    "text": entry.get("text", ""),
})
print(json.dumps(groups, ensure_ascii=False))
PY
)
            else
                sms_receive_print_entry "$index" "$status" "$line"
            fi
            found=true
            index=""
            status=""
        fi
    done <<< "$response"
    sms_receive_flush_concat_groups "$concat_groups"
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
