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
}

# 记录发送成功
# 用法: state_record_success
state_record_success() {
    state_init
    
    local now_ts=$(date +%s)
    local now_date=$(date '+%Y-%m-%d %H:%M:%S')
    
    # 记录时间戳
    echo "$now_ts" > "${STATE_DIR}/last_success"
    
    # 记录人类可读格式
    echo "$now_date" > "${STATE_DIR}/last_success_date"
    
    # 追加到历史记录
    echo "$now_ts|$now_date|success" >> "${STATE_DIR}/history.log"
    
    return 0
}

# 记录发送失败
# 用法: state_record_failure
state_record_failure() {
    state_init
    
    local now_ts=$(date +%s)
    local now_date=$(date '+%Y-%m-%d %H:%M:%S')
    
    # 追加到历史记录
    echo "$now_ts|$now_date|failure" >> "${STATE_DIR}/history.log"
    
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
    local last_ts=$(cat "$last_success_file")
    local now_ts=$(date +%s)
    local diff_seconds=$((now_ts - last_ts))
    local diff_days=$((diff_seconds / 86400))
    
    # 检查是否达到间隔
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
        cat "$last_success_file"
    else
        echo "0"
    fi
}

# 获取上次发送日期（人类可读）
# 用法: last_date=$(state_get_last_success_date)
state_get_last_success_date() {
    local last_success_file="${STATE_DIR}/last_success_date"
    
    if [[ -f "$last_success_file" ]]; then
        cat "$last_success_file"
    else
        echo "从未发送"
    fi
}

# 获取距今天数
# 用法: days=$(state_get_days_since)
state_get_days_since() {
    local last_ts=$(state_get_last_success)
    
    if [[ "$last_ts" == "0" ]]; then
        echo "-1"
        return
    fi
    
    local now_ts=$(date +%s)
    local diff_seconds=$((now_ts - last_ts))
    local diff_days=$((diff_seconds / 86400))
    
    echo "$diff_days"
}

# 获取剩余天数
# 用法: remaining=$(state_get_remaining 179)
state_get_remaining() {
    local interval_days="${1:-179}"
    local days_since=$(state_get_days_since)
    
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
    
    local days_since=$(state_get_days_since)
    if [[ "$days_since" == "-1" ]]; then
        echo "距今: 从未发送"
    else
        echo "距今: ${days_since} 天"
    fi
    
    local interval=$(config_read ".sms.interval_days" "179")
    local remaining=$(state_get_remaining "$interval")
    
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
    rm -f "${STATE_DIR}/last_success"
    rm -f "${STATE_DIR}/last_success_date"
    return 0
}
