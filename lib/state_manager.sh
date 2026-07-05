#!/bin/bash
# ============================================================================
# CardPulse - 状态管理模块
# 管理发送状态和历史记录
# ============================================================================

# 防止重复加载
[[ -n "${_STATE_MANAGER_LOADED:-}" ]] && return 0
_STATE_MANAGER_LOADED=1

# 状态目录
STATE_DIR="${CONFIG_DIR}/state"

# 初始化状态目录
state_init() {
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR" 2>/dev/null || true
}

# 原子写入文件（先写 .tmp 再 mv，避免断电/并发导致读取为空或损坏）
# 用法: atomic_write "文件路径" "内容"
atomic_write() {
    local file="$1"
    local content="$2"
    local tmpfile="${file}.tmp.$$"
    echo "$content" > "$tmpfile"
    chmod 600 "$tmpfile" 2>/dev/null || true
    mv "$tmpfile" "$file"
    chmod 600 "$file" 2>/dev/null || true
}

# 加锁追加到历史记录
# 使用 flock 防止并发写入交错
append_history() {
    local line="$1"
    local history_file="${STATE_DIR}/history.log"

    if command -v flock >/dev/null 2>&1; then
        (
            flock -x -w 5 200 || return 1
            echo "$line" >> "$history_file"
        ) 200>"${STATE_DIR}/history.lock"
    else
        echo "[WARN] 未找到 flock，历史记录追加未加锁（macOS 真机测试可接受）" >&2
        echo "$line" >> "$history_file"
    fi
}

# 记录发送成功
# 用法: state_record_success
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

# 记录发送失败
# 用法: state_record_failure
state_record_failure() {
    state_init
    
    local now_ts
    local now_date
    now_ts=$(date +%s)
    now_date=$(date '+%Y-%m-%d %H:%M:%S')

    append_history "$now_ts|$now_date|failure"
    
    return 0
}

# 判断是否需要发送
# 返回: 0=需要发送, 1=不需要
# 用法: state_should_send 179
state_should_send() {
    local interval_days="${1:-179}"
    local last_success_file="${STATE_DIR}/last_success"
    
    # 如果从未发送过，需要发送
    if [[ ! -f "$last_success_file" ]]; then
        return 0
    fi
    
    # 读取上次发送时间
    local last_ts
    local now_ts
    local diff_seconds
    local diff_days
    read -r last_ts < "$last_success_file"
    if [[ ! "$last_ts" =~ ^[0-9]+$ ]]; then
        echo "[WARN] 状态文件损坏，按从未发送处理: $last_success_file" >&2
        return 0
    fi
    now_ts=$(date +%s)
    diff_seconds=$((now_ts - last_ts))
    diff_days=$((diff_seconds / 86400))

    if [[ "$diff_days" -ge "$interval_days" ]]; then
        return 0
    else
        return 1
    fi
}

# 获取上次发送时间戳
# 用法: last_ts=$(state_get_last_success)
state_get_last_success() {
    local last_success_file="${STATE_DIR}/last_success"
    
    if [[ -f "$last_success_file" ]]; then
        local last_ts
        read -r last_ts < "$last_success_file"
        if [[ "$last_ts" =~ ^[0-9]+$ ]]; then
            echo "$last_ts"
        else
            echo "0"
        fi
    else
        echo "0"
    fi
}

# 获取上次发送日期（人类可读）
# 用法: last_date=$(state_get_last_success_date)
state_get_last_success_date() {
    local last_success_file="${STATE_DIR}/last_success_date"
    
    if [[ -f "$last_success_file" ]]; then
        local last_date
        read -r last_date < "$last_success_file"
        echo "$last_date"
    else
        echo "从未发送"
    fi
}

# 获取距今天数
# 用法: days=$(state_get_days_since)
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

# 获取剩余天数
# 用法: remaining=$(state_get_remaining 179)
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

# 显示状态信息
# 用法: state_show_status
state_show_status() {
    state_init
    
    echo "=== CardPulse 状态 ==="
    echo ""
    echo "上次发送: $(state_get_last_success_date)"
    
    local days_since
    days_since=$(state_get_days_since)
    if [[ "$days_since" == "-1" ]]; then
        echo "距今: 从未发送"
    else
        echo "距今: ${days_since} 天"
    fi
    
    local interval
    local remaining
    interval=$(config_read ".sms.interval_days" "179")
    remaining=$(state_get_remaining "$interval")

    if [[ "$remaining" -eq 0 ]]; then
        echo "状态: 需要发送"
    else
        echo "还需等待: ${remaining} 天"
    fi
    
    echo ""
    echo "配置间隔: ${interval} 天"
    
    # 显示最近历史
    local history_file="${STATE_DIR}/history.log"
    if [[ -f "$history_file" ]]; then
        echo ""
        echo "=== 最近记录 ==="
        tail -5 "$history_file" | while IFS='|' read -r ts date status; do
            echo "  $date - $status"
        done
    fi
}

# 清除状态（重新开始计时）
# 用法: state_reset
state_reset() {
    rm -f "${STATE_DIR}/last_success" "${STATE_DIR}/last_success_date"
    return 0
}
