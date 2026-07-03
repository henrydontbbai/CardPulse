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
    
    # 配置串口参数
    stty -F "$port" "$baudrate" raw -echo -echoe -echok 2>/dev/null
    if [[ $? -ne 0 ]]; then
        echo "[ERROR] 无法配置串口: $port" >&2
        return 1
    fi
    
    # 清空缓冲区
    cat "$port" > /dev/null 2>&1 &
    local cat_pid=$!
    sleep 0.1
    kill "$cat_pid" 2>/dev/null
    wait "$cat_pid" 2>/dev/null
    
    return 0
}

# 关闭串口
at_close() {
    AT_FD=""
    AT_PORT=""
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
    timeout 0.1 cat "$AT_PORT" > /dev/null 2>&1
    
    # 发送命令
    echo -ne "${command}\r" > "$AT_PORT"
    
    # 读取响应
    local response=""
    local start_time=$(date +%s)
    
    while true; do
        local current_time=$(date +%s)
        local elapsed=$((current_time - start_time))
        
        if [[ $elapsed -ge $timeout ]]; then
            break
        fi
        
        # 读取可用数据
        local data=$(timeout 0.1 cat "$AT_PORT" 2>/dev/null)
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
    local timeout="${1:-1}"
    
    if [[ -z "$AT_PORT" ]]; then
        echo "[ERROR] 串口未初始化" >&2
        return 1
    fi
    
    timeout "$timeout" cat "$AT_PORT" 2>/dev/null
}

# ============================================================================
# 状态检查函数
# ============================================================================

# 检查模组是否响应
at_check_modem() {
    local response=$(at_send "AT" 3)
    
    if echo "$response" | grep -q "OK"; then
        return 0
    else
        return 1
    fi
}

# 检查 SIM 卡状态
# 返回: READY, SIM PIN, SIM PUK, ERROR
at_check_sim() {
    local response=$(at_send "AT+CPIN?" 3)
    
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
    local response=$(at_send "AT+CSQ" 3)
    
    local rssi=$(echo "$response" | grep -oP '\+CSQ:\s*\K[0-9]+' | head -1)
    
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
    local response=$(at_send "AT+CREG?" 3)
    
    local status=$(echo "$response" | grep -oP '\+CREG:\s*\d+,\K\d' | head -1)
    
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
    local response=$(at_send "AT+COPS?" 5)
    
    local operator=$(echo "$response" | grep -oP '\+COPS:\s*\d+,\d+,"\K[^"]+' | head -1)
    
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
        ((errors++))
    fi
    
    # 检查 SIM 卡
    local sim_status=$(at_check_sim)
    if [[ "$sim_status" != "READY" ]]; then
        echo "[ERROR] SIM 卡状态异常: $sim_status" >&2
        ((errors++))
    fi
    
    # 检查信号
    local rssi=$(at_check_signal)
    if [[ "$rssi" == "99" ]]; then
        echo "[ERROR] 无信号" >&2
        ((errors++))
    elif [[ "$rssi" -lt 10 ]]; then
        echo "[WARN] 信号较弱: RSSI=$rssi" >&2
    fi
    
    # 检查网络注册
    local network_status=$(at_check_network)
    if [[ "$network_status" != "1" && "$network_status" != "5" ]]; then
        echo "[ERROR] 未注册网络: 状态=$network_status" >&2
        ((errors++))
    fi
    
    return $errors
}

# ============================================================================
# 短信发送函数
# ============================================================================

# TEXT 模式发送短信
# 用法: at_send_sms_text "+8613800138000" "Hello"
at_send_sms_text() {
    local phone="$1"
    local message="$2"
    local timeout="${3:-30}"
    
    # 设置 TEXT 模式
    at_send "AT+CMGF=1" 2
    at_send 'AT+CSCS="GSM"' 2
    
    # 发送 AT+CMGS
    echo -ne "AT+CMGS=\"${phone}\"\r" > "$AT_PORT"
    
    # 等待 > 提示符
    local response=""
    local start_time=$(date +%s)
    local prompt_received=false
    
    while true; do
        local current_time=$(date +%s)
        local elapsed=$((current_time - start_time))
        
        if [[ $elapsed -ge 10 ]]; then
            break
        fi
        
        local data=$(timeout 0.1 cat "$AT_PORT" 2>/dev/null)
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
    
    # 发送内容 + Ctrl+Z
    echo -ne "${message}\x1A" > "$AT_PORT"
    
    # 等待结果
    response=""
    start_time=$(date +%s)
    
    while true; do
        local current_time=$(date +%s)
        local elapsed=$((current_time - start_time))
        
        if [[ $elapsed -ge $timeout ]]; then
            break
        fi
        
        local data=$(timeout 0.1 cat "$AT_PORT" 2>/dev/null)
        if [[ -n "$data" ]]; then
            response="${response}${data}"
            
            if echo "$response" | grep -q "OK"; then
                # 提取消息引用编号
                local mr=$(echo "$response" | grep -oP '\+CMGS:\s*\K\d+' | head -1)
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
    local phone="$1"
    local message="$2"
    local timeout="${3:-30}"
    
    # 生成 PDU
    local pdu=$(at_generate_pdu "$phone" "$message")
    if [[ -z "$pdu" ]]; then
        echo "[ERROR] PDU 生成失败" >&2
        return 1
    fi
    
    local pdu_len=$((${#pdu} / 2))
    
    # 设置 PDU 模式
    at_send "AT+CMGF=0" 2
    
    # 发送 AT+CMGS
    echo -ne "AT+CMGS=${pdu_len}\r" > "$AT_PORT"
    
    # 等待 > 提示符
    local response=""
    local start_time=$(date +%s)
    local prompt_received=false
    
    while true; do
        local current_time=$(date +%s)
        local elapsed=$((current_time - start_time))
        
        if [[ $elapsed -ge 10 ]]; then
            break
        fi
        
        local data=$(timeout 0.1 cat "$AT_PORT" 2>/dev/null)
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
    echo -ne "${pdu}\x1A" > "$AT_PORT"
    
    # 等待结果
    response=""
    start_time=$(date +%s)
    
    while true; do
        local current_time=$(date +%s)
        local elapsed=$((current_time - start_time))
        
        if [[ $elapsed -ge $timeout ]]; then
            break
        fi
        
        local data=$(timeout 0.1 cat "$AT_PORT" 2>/dev/null)
        if [[ -n "$data" ]]; then
            response="${response}${data}"
            
            if echo "$response" | grep -q "OK"; then
                local mr=$(echo "$response" | grep -oP '\+CMGS:\s*\K\d+' | head -1)
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

# 生成 PDU 字符串
# 用法: pdu=$(at_generate_pdu "+8613800138000" "你好")
at_generate_pdu() {
    local phone="$1"
    local message="$2"
    
    # 移除 + 号
    phone="${phone#+}"
    
    # 检查是否包含非 ASCII 字符（需要 UCS2 编码）
    if echo "$message" | grep -qP '[^\x00-\x7F]'; then
        # UCS2 编码
        local encoded_message=$(echo -n "$message" | python3 -c "
import sys
text = sys.stdin.buffer.read().decode('utf-8')
pdu = ''
for char in text:
    code = ord(char)
    pdu += f'{code:04X}'
print(pdu)
" 2>/dev/null)
        
        # 构建 PDU
        # SMSC 长度为 0（使用默认 SMSC）
        local smsc_len="00"
        
        # SMS-SUBMIT 头部
        local mti="11"  # SMS-SUBMIT, TP-RD=1, TP-VPF=1
        local mr="00"   # 消息引用
        local da_len=$(printf "%02X" $((${#phone})))
        local da_type="91"  # 国际格式
        
        # 交换号码对
        local da=""
        if [[ $((${#phone} % 2)) -eq 1 ]]; then
            phone="${phone}F"
        fi
        for ((i=0; i<${#phone}; i+=2)); do
            da="${da}${phone:$((i+1)):1}${phone:$i:1}"
        done
        
        local pid="00"
        local dcs="08"  # UCS2
        local vpf="AA"  # 相对格式
        local vp="A7"   # 24小时有效期
        local udl=$(printf "%02X" $((${#encoded_message} / 2)))
        
        echo "${smsc_len}${mti}${mr}${da_len}${da_type}${da}${pid}${dcs}${vpf}${vp}${udl}${encoded_message}"
    else
        # GSM 7-bit 编码
        local encoded_message=$(echo -n "$message" | python3 -c "
import sys
text = sys.stdin.buffer.read().decode('ascii')
# GSM 7-bit 编码（简化版，仅支持基础字符集）
gsm_charset = '@£\$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ ÆæßÉ !\"#¤%&\'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZ'
pdu = ''
for char in text:
    idx = gsm_charset.find(char)
    if idx >= 0:
        pdu += format(idx, '07b')
    else:
        pdu += format(ord(char), '07b')
# 补齐到完整字节
while len(pdu) % 8 != 0:
    pdu += '0'
# 转换为十六进制
result = ''
for i in range(0, len(pdu), 8):
    byte = pdu[i:i+8]
    result += format(int(byte, 2), '02X')
print(result)
" 2>/dev/null)
        
        # 构建 PDU
        local smsc_len="00"
        local mti="11"
        local mr="00"
        local da_len=$(printf "%02X" $((${#phone})))
        local da_type="91"
        
        local da=""
        if [[ $((${#phone} % 2)) -eq 1 ]]; then
            phone="${phone}F"
        fi
        for ((i=0; i<${#phone}; i+=2)); do
            da="${da}${phone:$((i+1)):1}${phone:$i:1}"
        done
        
        local pid="00"
        local dcs="00"  # GSM 7-bit
        local vpf="AA"
        local vp="A7"
        local udl=$(printf "%02X" $((${#message})))
        
        echo "${smsc_len}${mti}${mr}${da_len}${da_type}${da}${pid}${dcs}${vpf}${vp}${udl}${encoded_message}"
    fi
}

# 检测串口设备
# 返回: 第一个找到的 /dev/ttyUSB* 设备
at_detect_device() {
    local devices=$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | head -1)
    
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
    
    local manufacturer=$(at_send "AT+CGMI" 2 | grep -v "^AT" | tr -d '\r')
    local model=$(at_send "AT+CGMM" 2 | grep -v "^AT" | tr -d '\r')
    local imei=$(at_send "AT+CGSN" 2 | grep -v "^AT" | tr -d '\r')
    local version=$(at_send "AT+CGMR" 2 | grep -v "^AT" | tr -d '\r')
    
    echo "厂商: $manufacturer"
    echo "型号: $model"
    echo "IMEI: $imei"
    echo "版本: $version"
    echo ""
    echo "=== 状态信息 ==="
    
    local sim=$(at_check_sim)
    local rssi=$(at_check_signal)
    local network=$(at_check_network)
    local operator=$(at_check_operator)
    
    echo "SIM 卡: $sim"
    echo "信号强度: $rssi"
    echo "网络状态: $network"
    echo "运营商: $operator"
}
