#!/bin/bash
# CardPulse - local send state helpers.

[[ -n "${_STATE_MANAGER_LOADED:-}" ]] && return 0
_STATE_MANAGER_LOADED=1

STATE_DIR="${CARDPULSE_STATE_DIR:-${CONFIG_DIR}/state}"

state_init() {
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR" 2>/dev/null || true
}

atomic_write() {
    local file="$1"
    local content="$2"
    local tmpfile="${file}.tmp.$$"
    echo "$content" > "$tmpfile"
    chmod 600 "$tmpfile" 2>/dev/null || true
    mv "$tmpfile" "$file"
    chmod 600 "$file" 2>/dev/null || true
}

append_history() {
    local line="$1"
    local history_file="${STATE_DIR}/history.log"

    if command -v flock >/dev/null 2>&1; then
        (
            flock -x -w 5 200 || return 1
            echo "$line" >> "$history_file"
        ) 200>"${STATE_DIR}/history.lock"
    else
        echo "[WARN] flock not found; history append is unlocked for local macOS testing" >&2
        echo "$line" >> "$history_file"
    fi
}

state_record_success() {
    state_init

    local now_ts
    local now_date
    now_ts=$(date +%s)
    now_date=$(date '+%Y-%m-%d %H:%M:%S')

    atomic_write "${STATE_DIR}/last_success" "$now_ts"
    atomic_write "${STATE_DIR}/last_success_date" "$now_date"
    append_history "$now_ts|$now_date|success"
    return 0
}

state_record_failure() {
    state_init

    local now_ts
    local now_date
    now_ts=$(date +%s)
    now_date=$(date '+%Y-%m-%d %H:%M:%S')

    append_history "$now_ts|$now_date|failure"
    return 0
}

state_should_send() {
    local interval_days="${1:-179}"
    local last_success_file="${STATE_DIR}/last_success"

    if [[ ! -f "$last_success_file" ]]; then
        return 0
    fi

    local last_ts
    local now_ts
    local diff_seconds
    local diff_days
    read -r last_ts < "$last_success_file"
    if [[ ! "$last_ts" =~ ^[0-9]+$ ]]; then
        echo "[WARN] State file is corrupted; treating as never sent: $last_success_file" >&2
        return 0
    fi

    now_ts=$(date +%s)
    diff_seconds=$((now_ts - last_ts))
    diff_days=$((diff_seconds / 86400))

    if [[ "$diff_days" -ge "$interval_days" ]]; then
        return 0
    fi
    return 1
}

state_get_last_success() {
    local last_success_file="${STATE_DIR}/last_success"

    if [[ -f "$last_success_file" ]]; then
        local last_ts
        read -r last_ts < "$last_success_file"
        if [[ "$last_ts" =~ ^[0-9]+$ ]]; then
            echo "$last_ts"
            return
        fi
    fi
    echo "0"
}

state_get_last_success_date() {
    local last_success_file="${STATE_DIR}/last_success_date"

    if [[ -f "$last_success_file" ]]; then
        local last_date
        read -r last_date < "$last_success_file"
        echo "$last_date"
        return
    fi
    echo "never"
}

state_get_days_since() {
    local last_ts
    last_ts=$(state_get_last_success)

    if [[ "$last_ts" == "0" ]]; then
        echo "-1"
        return
    fi

    local now_ts
    local diff_seconds
    local diff_days
    now_ts=$(date +%s)
    diff_seconds=$((now_ts - last_ts))
    diff_days=$((diff_seconds / 86400))
    echo "$diff_days"
}

state_get_remaining() {
    local interval_days="${1:-179}"
    local days_since
    days_since=$(state_get_days_since)

    if [[ "$days_since" == "-1" ]]; then
        echo "0"
        return
    fi

    local remaining=$((interval_days - days_since))
    if [[ $remaining -lt 0 ]]; then
        echo "0"
    else
        echo "$remaining"
    fi
}

state_get_last_result() {
    local history_file="${STATE_DIR}/history.log"
    if [[ ! -f "$history_file" ]]; then
        echo "unknown"
        return
    fi

    local last_entry
    last_entry=$(tail -1 "$history_file" 2>/dev/null || true)
    if [[ -z "$last_entry" ]]; then
        echo "unknown"
        return
    fi
    echo "${last_entry##*|}"
}

state_format_next_send() {
    local interval_days="$1"
    local remaining="$2"
    local last_ts
    last_ts=$(state_get_last_success)

    if [[ "$last_ts" == "0" ]]; then
        echo "due now"
        return
    fi

    if [[ "$remaining" -eq 0 ]]; then
        echo "due now"
        return
    fi

    local target_ts=$((last_ts + interval_days * 86400))
    if next_send=$(date -d "@$target_ts" '+%Y-%m-%d %H:%M:%S' 2>/dev/null); then
        echo "$next_send"
    else
        echo "in ${remaining} days"
    fi
}

state_show_status() {
    state_init

    local interval_days
    interval_days=$(config_read ".sms.interval_days" "179")
    local days_since
    days_since=$(state_get_days_since)
    local remaining_days
    remaining_days=$(state_get_remaining "$interval_days")
    local send_due="yes"
    if [[ "$remaining_days" -gt 0 && "$days_since" != "-1" ]]; then
        send_due="no"
    fi

    echo "=== CardPulse Status ==="
    echo "Last send: $(state_get_last_success_date)"
    echo "Days since last send: $days_since"
    echo "Interval days: $interval_days"
    echo "Remaining days: $remaining_days"
    echo "Send due: $send_due"
    echo "Next send: $(state_format_next_send "$interval_days" "$remaining_days")"
    echo "Last result: $(state_get_last_result)"

    local history_file="${STATE_DIR}/history.log"
    if [[ -f "$history_file" ]]; then
        echo ""
        echo "=== Recent history ==="
        tail -5 "$history_file" | while IFS='|' read -r ts date status; do
            echo "  $date - $status"
        done
    fi
}

state_reset() {
    rm -f "${STATE_DIR}/last_success" "${STATE_DIR}/last_success_date"
    return 0
}
