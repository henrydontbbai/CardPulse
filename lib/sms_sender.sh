#!/bin/bash
# ============================================================================
# CardPulse - 短信发送模块
# 支持 TEXT 模式和 PDU 模式（中文）
# ============================================================================

# 防止重复加载
[[ -n "${_SMS_SENDER_LOADED:-}" ]] && return 0
_SMS_SENDER_LOADED=1

if ! command -v at_list_candidate_devices >/dev/null 2>&1; then
    if [[ -n "${CARDPULSE_LIB_DIR:-}" && -f "${CARDPULSE_LIB_DIR}/at_modem.sh" ]]; then
        source "${CARDPULSE_LIB_DIR}/at_modem.sh"
    else
        source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/at_modem.sh"
    fi
fi

# 检测串口设备
# 优先使用配置中的端口，否则自动检测
sms_detect_device() {
    local configured_port
    local auto_detect
    configured_port=$(config_read ".serial.port" "")
    auto_detect=$(config_read ".serial.auto_detect" "true")

    # 如果配置了端口且存在，使用配置的端口
    if [[ -n "$configured_port" && -e "$configured_port" ]]; then
        echo "$configured_port"
        return 0
    fi
    
    # 自动检测
    if ! config_is_true "$auto_detect"; then
        return 1
    fi

    local detected
    detected=$(at_detect_device) || detected=""
    if [[ -n "$detected" ]]; then
        echo "$detected"
        return 0
    fi

    return 1
}

# 检查是否需要使用 PDU 模式
# PDU 编码更可控，默认用于所有消息以避免不同模组 TEXT 字符集差异。
sms_needs_pdu() {
    return 0
}

# 发送短信（自动选择模式）
# 用法: sms_send "+8613800138000" "Hello" 或 sms_send "+8613800138000" "你好"
sms_send() {
    local phone="$1"
    local message="$2"
    local timeout
    timeout=$(config_read ".sms.timeout" "30")

    # 发送短信
    local result
    local exit_code=0
    if sms_needs_pdu "$message"; then
        result=$(at_send_sms_pdu "$phone" "$message" "$timeout")
    else
        result=$(at_send_sms_text "$phone" "$message" "$timeout")
    fi || exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        echo "$result"
        return 0
    else
        return 1
    fi
}

# 带重试的短信发送
# 用法: sms_send_with_retry "+8613800138000" "Hello"
sms_send_with_retry() {
    local phone="$1"
    local message="$2"
    local max_attempts
    local retry_interval
    max_attempts=$(config_read ".retry.max_attempts" "3")
    retry_interval=$(config_read ".retry.interval" "10")
    local attempt=1

    while [[ $attempt -le $max_attempts ]]; do
        local result
        local exit_code=0
        result=$(sms_send "$phone" "$message") || exit_code=$?

        if [[ $exit_code -eq 0 ]]; then
            echo "$result"
            return 0
        fi
        
        if [[ $attempt -lt $max_attempts ]]; then
            echo "[WARN] 发送失败，${retry_interval} 秒后重试 (${attempt}/${max_attempts})..." >&2
            sleep "$retry_interval"
        fi
        
        ((attempt+=1))
    done
    
    echo "[ERROR] 发送失败: 已达最大重试次数" >&2
    return 1
}

# 验证前置条件
sms_validate_preconditions() {
    echo "[INFO] 验证前置条件..." >&2

    # 检查模组响应
    if ! at_check_modem; then
        echo "[ERROR] 模组无响应" >&2
        return 1
    fi
    echo "[INFO] ✓ 模组响应正常" >&2
    
    # 检查 SIM 卡
    local sim_status
    sim_status=$(at_check_sim) || sim_status="ERROR"
    if [[ "$sim_status" != "READY" ]]; then
        echo "[ERROR] SIM 卡状态异常: $sim_status" >&2
        return 1
    fi
    echo "[INFO] ✓ SIM 卡就绪" >&2
    
    # 检查信号
    local rssi
    rssi=$(at_check_signal) || rssi="99"
    if [[ "$rssi" == "99" ]]; then
        echo "[ERROR] 无信号" >&2
        return 1
    fi
    if [[ "$rssi" -lt 10 ]]; then
        echo "[WARN] 信号较弱: RSSI=$rssi" >&2
    else
        echo "[INFO] ✓ 信号正常: RSSI=$rssi" >&2
    fi
    
    # 检查网络注册
    local network_status
    network_status=$(at_check_network) || network_status="0"
    if [[ "$network_status" != "1" && "$network_status" != "5" ]]; then
        echo "[ERROR] 未注册网络: 状态=$network_status" >&2
        return 1
    fi
    echo "[INFO] ✓ 网络已注册" >&2
    
    return 0
}

# 发送测试短信
# 用法: sms_send_test
sms_send_test() {
    local phone
    local message
    phone=$(config_read ".sms.phone" "")
    message=$(config_read ".sms.message" "Hello from CardPulse")

    if [[ -z "$phone" ]]; then
        echo "[ERROR] 未配置短信接收号码" >&2
        return 1
    fi
    
    local phone_masked
    phone_masked=$(config_mask_string "$phone")
    echo "[INFO] 发送测试短信..." >&2
    echo "[INFO] 目标号码: $phone_masked" >&2
    echo "[INFO] 消息长度: ${#message} 字符" >&2
    
    # 检测串口设备
    local device
    device=$(sms_detect_device) || device=""
    if [[ -z "$device" ]]; then
        echo "[ERROR] 未找到串口设备" >&2
        return 1
    fi
    echo "[INFO] 使用设备: $device" >&2
    
    # 获取串口参数
    local baudrate
    baudrate=$(config_read ".serial.baudrate" "115200")

    # 初始化串口
    if ! at_init "$device" "$baudrate"; then
        echo "[ERROR] 串口初始化失败" >&2
        return 1
    fi
    
    # 验证前置条件
    if ! sms_validate_preconditions; then
        at_close
        return 1
    fi
    
    # 发送短信
    local result
    local exit_code=0
    result=$(sms_send_with_retry "$phone" "$message") || exit_code=$?

    at_close
    
    if [[ $exit_code -eq 0 ]]; then
        echo "[INFO] ✓ 短信发送成功" >&2
        echo "[INFO] 消息引用: $result" >&2
        return 0
    else
        echo "[ERROR] 短信发送失败" >&2
        return 1
    fi
}
