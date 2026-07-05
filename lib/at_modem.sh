#!/bin/bash
# ============================================================================
# CardPulse - AT 命令通信模块
# 通过串口直接控制 4G 模组
# ============================================================================

# 防止重复加载
[[ -n "${_AT_MODEM_LOADED:-}" ]] && return 0
_AT_MODEM_LOADED=1

# 串口设备文件描述符
AT_FD=""
AT_PORT=""
AT_BAUDRATE=""
AT_CAT_PID=""
AT_LOCKED=false

at_timeout() {
    local duration="$1"
    shift

    if command -v timeout >/dev/null 2>&1; then
        timeout "$duration" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$duration" "$@"
    else
        echo "[ERROR] 需要 timeout 命令；macOS 请执行: brew install coreutils" >&2
        return 127
    fi
}

at_has_timeout() {
    command -v timeout >/dev/null 2>&1 || command -v gtimeout >/dev/null 2>&1
}

at_configure_stty() {
    local port="$1"
    local baudrate="$2"

    if stty -F "$port" "$baudrate" raw -echo -echoe -echok 2>/dev/null; then
        return 0
    fi
    if stty -f "$port" "$baudrate" raw -echo -echoe -echok 2>/dev/null; then
        return 0
    fi
    return 1
}

# 初始化串口
# 用法: at_init "/dev/ttyUSB0" "115200"
at_init() {
    local port="$1"
    local baudrate="${2:-115200}"

    AT_PORT="$port"
    AT_BAUDRATE="$baudrate"

    # 检查设备是否存在
    if [[ ! -e "$port" ]]; then
        echo "[ERROR] 串口设备不存在: $port" >&2
        return 1
    fi

    # 检查权限
    if [[ ! -r "$port" || ! -w "$port" ]]; then
        echo "[ERROR] 无权访问串口设备: $port (需要 dialout 组权限)" >&2
        return 1
    fi

    if ! at_has_timeout; then
        echo "[ERROR] 未找到 timeout/gtimeout；macOS 请执行: brew install coreutils" >&2
        return 1
    fi

    # 以独占方式打开串口（获取文件描述符+flock）
    exec 3<> "$port" 2>/dev/null || {
        echo "[ERROR] 无法打开串口: $port" >&2
        return 1
    }
    AT_FD=3

    # 获取串口互斥锁（阻塞等待最多 30 秒）
    if command -v flock >/dev/null 2>&1; then
        if ! flock -x -w 30 "$AT_FD" 2>/dev/null; then
            echo "[ERROR] 无法获取串口锁（设备忙）: $port" >&2
            exec 3>&-
            AT_FD=""
            return 1
        fi
        AT_LOCKED=true
    else
        echo "[WARN] 未找到 flock，跳过串口互斥锁（macOS 真机测试可接受）" >&2
    fi

    # 配置串口参数
    if ! at_configure_stty "$port" "$baudrate"; then
        echo "[ERROR] 无法配置串口: $port" >&2
        at_close
        return 1
    fi

    # 清空缓冲区（后台 cat，记录 PID 回头清理）
    cat "$port" > /dev/null 2>&1 &
    AT_CAT_PID=$!
    sleep 0.1
    kill "$AT_CAT_PID" 2>/dev/null
    wait "$AT_CAT_PID" 2>/dev/null
    AT_CAT_PID=""

    return 0
}

# 关闭串口
at_close() {
    # 清理后台 cat 进程
    if [[ -n "${AT_CAT_PID:-}" ]]; then
        kill "$AT_CAT_PID" 2>/dev/null || true
        wait "$AT_CAT_PID" 2>/dev/null || true
        AT_CAT_PID=""
    fi
    # 释放串口锁并关闭文件描述符
    if [[ -n "${AT_FD:-}" ]]; then
        if [[ "$AT_LOCKED" == "true" ]]; then
            flock -u "$AT_FD" 2>/dev/null || true
        fi
        exec 3>&- 2>/dev/null || true
    fi
    AT_FD=""
    AT_LOCKED=false
    AT_PORT=""
    AT_BAUDRATE=""
}

# 发送 AT 命令并返回响应
# 用法: response=$(at_send "AT" 5)
at_send() {
    local command="$1"
    local timeout="${2:-5}"

    if [[ -z "$AT_PORT" ]]; then
        echo "[ERROR] 串口未初始化" >&2
        return 1
    fi

    # 清空输入缓冲区
    at_timeout 0.1 cat "$AT_PORT" > /dev/null 2>&1 || true

    # 发送命令
    echo -ne "${command}\r" > "$AT_PORT"
    
    # 读取响应
    local response=""
    local start_time
    start_time=$(date +%s)

    while true; do
        local current_time
        current_time=$(date +%s)
        local elapsed=$((current_time - start_time))

        if [[ $elapsed -ge $timeout ]]; then
            break
        fi
        
        # 读取可用数据
        local data
        data=$(at_timeout 0.1 cat "$AT_PORT" 2>/dev/null || true)
        if [[ -n "$data" ]]; then
            response="${response}${data}"

            # 检查是否收到完整响应
            if echo "$response" | grep -qE "OK|ERROR|RING|CONNECT"; then
                break
            fi
        fi

        sleep 0.05
    done
    
    echo "$response"
}

# 快速发送命令（不等待完整响应）
at_send_quick() {
    local command="$1"
    
    if [[ -z "$AT_PORT" ]]; then
        echo "[ERROR] 串口未初始化" >&2
        return 1
    fi
    
    echo -ne "${command}\r" > "$AT_PORT"
    sleep 0.1
}

# 读取串口数据（带超时）
at_read() {
    local read_timeout="${1:-1}"
    
    if [[ -z "$AT_PORT" ]]; then
        echo "[ERROR] 串口未初始化" >&2
        return 1
    fi
    
    at_timeout "$read_timeout" cat "$AT_PORT" 2>/dev/null
}

# ============================================================================
# 状态检查函数
# ============================================================================

# 检查模组是否响应
at_check_modem() {
    local response
    response=$(at_send "AT" 3) || response=""

    if echo "$response" | grep -q "OK"; then
        return 0
    else
        return 1
    fi
}

# 检查 SIM 卡状态
# 返回: READY, SIM PIN, SIM PUK, ERROR
at_check_sim() {
    local response
    response=$(at_send "AT+CPIN?" 3) || response=""

    if echo "$response" | grep -q "READY"; then
        echo "READY"
        return 0
    elif echo "$response" | grep -q "SIM PIN"; then
        echo "SIM PIN"
        return 1
    elif echo "$response" | grep -q "SIM PUK"; then
        echo "SIM PUK"
        return 1
    else
        echo "ERROR"
        return 1
    fi
}

# 检查信号强度
# 返回: RSSI 值 (0-31, 99=未知)
at_check_signal() {
    local response
    local rssi
    response=$(at_send "AT+CSQ" 3) || response=""
    rssi=$(echo "$response" | LC_ALL=C grep -o '+CSQ: *[0-9]*' | grep -o '[0-9]*' | head -1 || true)

    if [[ -n "$rssi" ]]; then
        echo "$rssi"
        return 0
    else
        echo "99"
        return 1
    fi
}

# 检查网络注册状态
# 返回: 0=未注册, 1=已注册本地, 2=搜索中, 5=已注册漫游
at_check_network() {
    local response
    local status
    response=$(at_send "AT+CREG?" 3) || response=""
    status=$(echo "$response" | LC_ALL=C grep -o '+CREG: *[0-9]*,[0-9]*' | grep -o '[0-9]*$' | head -1 || true)

    if [[ -n "$status" ]]; then
        echo "$status"
        return 0
    else
        echo "0"
        return 1
    fi
}

# 检查运营商信息
at_check_operator() {
    local response
    local operator
    response=$(at_send "AT+COPS?" 5) || response=""
    operator=$(echo "$response" | LC_ALL=C grep -o '+COPS: *[0-9]*,[0-9]*,"[^"]*"' | head -1 | sed 's/+COPS: *[0-9]*,[0-9]*,*"\(.*\)"/\1/' || true)

    if [[ -n "$operator" ]]; then
        echo "$operator"
        return 0
    else
        echo "未知"
        return 1
    fi
}

# 验证所有前置条件
at_validate_preconditions() {
    local errors=0
    
    # 检查模组响应
    if ! at_check_modem; then
        echo "[ERROR] 模组无响应" >&2
        ((errors+=1))
    fi
    
    # 检查 SIM 卡
    local sim_status
    sim_status=$(at_check_sim) || sim_status="ERROR"
    if [[ "$sim_status" != "READY" ]]; then
        echo "[ERROR] SIM 卡状态异常: $sim_status" >&2
        ((errors+=1))
    fi
    
    # 检查信号
    local rssi
    rssi=$(at_check_signal) || rssi="99"
    if [[ "$rssi" == "99" ]]; then
        echo "[ERROR] 无信号" >&2
        ((errors+=1))
    elif [[ "$rssi" -lt 10 ]]; then
        echo "[WARN] 信号较弱: RSSI=$rssi" >&2
    fi
    
    # 检查网络注册
    local network_status
    network_status=$(at_check_network) || network_status="0"
    if [[ "$network_status" != "1" && "$network_status" != "5" ]]; then
        echo "[ERROR] 未注册网络: 状态=$network_status" >&2
        ((errors+=1))
    fi
    
    return "$errors"
}

# ============================================================================
# 短信发送函数
# ============================================================================

# 验证 AT 命令响应包含 OK
# 用法: at_expect_ok "AT+CMGF=1" 2
at_expect_ok() {
    local cmd="$1"
    local timeout="${2:-3}"
    local response
    response=$(at_send "$cmd" "$timeout")
    if echo "$response" | grep -q "OK"; then
        return 0
    fi
    echo "[ERROR] AT 命令失败: $cmd (响应: $(echo "$response" | tr -d '\r\n'))" >&2
    return 1
}

# 消毒号码：仅保留 + 和数字
sanitize_phone() {
    local phone="$1"
    phone="${phone//[[:space:].()-]/}"
    if [[ ! "$phone" =~ ^\+?[0-9]{5,15}$ ]]; then
        return 1
    fi
    echo "$phone"
    return 0
}

# 消毒消息：移除控制字符
sanitize_message() {
    local msg="$1"
    msg=$(printf '%s' "$msg" | python3 -c 'import re,sys; print(re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1A\x1C-\x1F]", "", sys.stdin.read()), end="")')
    echo "$msg"
}

at_resolve_pdu_encoder() {
    local encoder=""
    if [[ -n "${CARDPULSE_LIB_DIR:-}" && -f "${CARDPULSE_LIB_DIR}/pdu_encoder.py" ]]; then
        encoder="${CARDPULSE_LIB_DIR}/pdu_encoder.py"
    else
        encoder="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pdu_encoder.py"
    fi
    if [[ ! -f "$encoder" ]]; then
        encoder="/opt/cardpulse/lib/pdu_encoder.py"
    fi
    if [[ ! -f "$encoder" ]]; then
        return 1
    fi
    echo "$encoder"
}

# TEXT 模式发送短信
# 用法: at_send_sms_text "+8613800138000" "Hello"
at_send_sms_text() {
    local raw_phone="$1"
    local raw_message="$2"
    local timeout="${3:-30}"
    
    local phone
    phone=$(sanitize_phone "$raw_phone") || {
        echo "[ERROR] 无效的电话号码: $raw_phone" >&2
        return 1
    }
    local message
    message=$(sanitize_message "$raw_message")
    if [[ -z "$message" ]]; then
        echo "[ERROR] 短信内容为空" >&2
        return 1
    fi

    # 设置 TEXT 模式（验证响应）
    at_expect_ok "AT+CMGF=1" 2 || return 1
    at_expect_ok 'AT+CSCS="GSM"' 2 || return 1
    
    # 发送 AT+CMGS
    echo -ne "AT+CMGS=\"${phone}\"\r" > "$AT_PORT"
    
    # 等待 > 提示符
    local response=""
    local start_time
    start_time=$(date +%s)
    local prompt_received=false
    
    while true; do
        local current_time
        current_time=$(date +%s)
        local elapsed=$((current_time - start_time))
        
        if [[ $elapsed -ge 10 ]]; then
            break
        fi
        
        local data
        data=$(at_timeout 0.1 cat "$AT_PORT" 2>/dev/null || true)
        if [[ -n "$data" ]]; then
            response="${response}${data}"
            
            if echo "$response" | grep -q ">"; then
                prompt_received=true
                break
            fi
        fi
        
        sleep 0.05
    done
    
    if [[ "$prompt_received" != "true" ]]; then
        echo "[ERROR] 未收到 > 提示符" >&2
        return 1
    fi
    
    # 发送内容 + Ctrl+Z（消毒后的消息确保不含 \x1A）
    echo -ne "${message}\x1A" > "$AT_PORT"
    
    # 等待结果
    response=""
    start_time=$(date +%s)
    
    while true; do
        local current_time
        current_time=$(date +%s)
        local elapsed=$((current_time - start_time))
        
        if [[ $elapsed -ge $timeout ]]; then
            break
        fi
        
        local data
        data=$(at_timeout 0.1 cat "$AT_PORT" 2>/dev/null || true)
        if [[ -n "$data" ]]; then
            response="${response}${data}"
            
            if echo "$response" | grep -q "OK"; then
                local mr
                mr=$(echo "$response" | LC_ALL=C grep -o '\+CMGS: *[0-9]*' | grep -o '[0-9]*' || true)
                echo "$mr"
                return 0
            fi
            
            if echo "$response" | grep -q "ERROR"; then
                echo "[ERROR] 发送失败: $response" >&2
                return 1
            fi
        fi
        
        sleep 0.05
    done
    
    echo "[ERROR] 发送超时" >&2
    return 1
}

# PDU 模式发送短信（支持中文）
# 用法: at_send_sms_pdu "+8613800138000" "你好"
at_send_sms_pdu() {
    local raw_phone="$1"
    local raw_message="$2"
    local timeout="${3:-30}"
    
    local phone
    phone=$(sanitize_phone "$raw_phone") || {
        echo "[ERROR] 无效的电话号码: $raw_phone" >&2
        return 1
    }
    local message
    message=$(sanitize_message "$raw_message")
    if [[ -z "$message" ]]; then
        echo "[ERROR] 短信内容为空" >&2
        return 1
    fi
    
    # 调用 Python PDU 编码器
    local PDU_ENCODER
    if ! PDU_ENCODER=$(at_resolve_pdu_encoder); then
        echo "[ERROR] 未找到 PDU 编码器: pdu_encoder.py" >&2
        return 1
    fi
    
    local pdu_output
    local exit_code=0
    pdu_output=$(python3 "$PDU_ENCODER" "$phone" "$message" 2>&1) || exit_code=$?

    if [[ $exit_code -ne 0 ]]; then
        echo "[ERROR] PDU 编码失败: $pdu_output" >&2
        return 1
    fi
    
    # 解析输出: <pdu_hex> <tpdu_octet_count>
    local pdu_hex="${pdu_output% *}"
    local tpdu_len="${pdu_output##* }"

    # 验证 PDU 格式
    if [[ -z "$pdu_hex" || $((${#pdu_hex} % 2)) -ne 0 ]]; then
        echo "[ERROR] 生成的 PDU 格式无效" >&2
        return 1
    fi
    if ! echo "$pdu_hex" | LC_ALL=C grep -q '^[0-9A-Fa-f]\+$'; then
        echo "[ERROR] 生成的 PDU 包含非法字符" >&2
        return 1
    fi

    # 设置 PDU 模式（验证响应）
    at_expect_ok "AT+CMGF=0" 2 || return 1

    # 发送 AT+CMGS（使用 TPDU 长度，不含 SMSC 地址字节）
    echo -ne "AT+CMGS=${tpdu_len}\r" > "$AT_PORT"
    
    # 等待 > 提示符
    local response=""
    local start_time
    start_time=$(date +%s)
    local prompt_received=false
    
    while true; do
        local current_time
        current_time=$(date +%s)
        local elapsed=$((current_time - start_time))
        
        if [[ $elapsed -ge 10 ]]; then
            break
        fi
        
        local data
        data=$(at_timeout 0.1 cat "$AT_PORT" 2>/dev/null || true)
        if [[ -n "$data" ]]; then
            response="${response}${data}"
            
            if echo "$response" | grep -q ">"; then
                prompt_received=true
                break
            fi
        fi
        
        sleep 0.05
    done
    
    if [[ "$prompt_received" != "true" ]]; then
        echo "[ERROR] 未收到 > 提示符" >&2
        return 1
    fi
    
    # 发送 PDU + Ctrl+Z
    echo -ne "${pdu_hex}\x1A" > "$AT_PORT"
    
    # 等待结果
    response=""
    start_time=$(date +%s)
    
    while true; do
        local current_time
        current_time=$(date +%s)
        local elapsed=$((current_time - start_time))
        
        if [[ $elapsed -ge $timeout ]]; then
            break
        fi
        
        local data
        data=$(at_timeout 0.1 cat "$AT_PORT" 2>/dev/null || true)
        if [[ -n "$data" ]]; then
            response="${response}${data}"
            
            if echo "$response" | grep -q "OK"; then
                local mr
                mr=$(echo "$response" | LC_ALL=C grep -o '\+CMGS: *[0-9]*' | grep -o '[0-9]*' || true)
                echo "$mr"
                return 0
            fi
            
            if echo "$response" | grep -q "ERROR"; then
                echo "[ERROR] 发送失败: $response" >&2
                return 1
            fi
        fi
        
        sleep 0.05
    done
    
    echo "[ERROR] 发送超时" >&2
    return 1
}

# 检测串口设备
# 返回: 第一个找到的 Linux/macOS USB 串口设备
at_detect_device() {
    local devices
    local pattern
    local device

    for pattern in /dev/ttyUSB* /dev/ttyACM* /dev/cu.usbserial* /dev/cu.usbmodem* /dev/cu.wchusbserial* /dev/cu.SLAB_USBtoUART*; do
        for device in $pattern; do
            if [[ -e "$device" ]]; then
                devices="$device"
                break 2
            fi
        done
    done

    if [[ -n "$devices" ]]; then
        echo "$devices"
        return 0
    else
        return 1
    fi
}

# 显示模组信息
at_show_info() {
    echo "=== 模组信息 ==="
    echo "设备: $AT_PORT"
    echo "波特率: $AT_BAUDRATE"
    echo ""
    
    local manufacturer
    local model
    local imei
    local version
    manufacturer=$(at_send "AT+CGMI" 2 | LC_ALL=C grep -v "^AT" | tr -d '\r' | head -1 || true)
    model=$(at_send "AT+CGMM" 2 | LC_ALL=C grep -v "^AT" | tr -d '\r' | head -1 || true)
    imei=$(at_send "AT+CGSN" 2 | LC_ALL=C grep -v "^AT" | tr -d '\r' | head -1 || true)
    version=$(at_send "AT+CGMR" 2 | LC_ALL=C grep -v "^AT" | tr -d '\r' | head -1 || true)

    [[ -z "$manufacturer" ]] && manufacturer="(无法获取)"
    [[ -z "$model" ]] && model="(无法获取)"
    [[ -z "$imei" ]] && imei="(无法获取)"
    [[ -z "$version" ]] && version="(无法获取)"
    
    echo "厂商: $manufacturer"
    echo "型号: $model"
    echo "IMEI: $imei"
    echo "版本: $version"
    echo ""
    echo "=== 状态信息 ==="
    
    local sim
    local rssi
    local network
    local operator
    sim=$(at_check_sim) || sim="ERROR"
    rssi=$(at_check_signal) || rssi="99"
    network=$(at_check_network) || network="0"
    operator=$(at_check_operator) || operator="未知"

    echo "SIM 卡: $sim"
    echo "信号强度: $rssi"
    echo "网络状态: $network"
    echo "运营商: $operator"
}
