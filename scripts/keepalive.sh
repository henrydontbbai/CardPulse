#!/bin/bash
# ============================================================================
# CardPulse - SIM卡定时保号工具
# 让每一张 SIM 卡都有跳动的脉搏
# ============================================================================

set -euo pipefail

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 默认配置目录
CONFIG_DIR="${CARDPULSE_CONFIG_DIR:-$HOME/.cardpulse}"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"
STATE_DIR="${CONFIG_DIR}/state"
LOG_DIR="${CONFIG_DIR}/logs"

# 默认值
DEFAULT_INTERVAL_DAYS=179
DEFAULT_MESSAGE="Hello from CardPulse"

# ============================================================================
# 函数定义
# ============================================================================

log() {
    local level="$1"
    shift
    local msg="$*"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo -e "${timestamp} [${level}] ${msg}" | tee -a "${LOG_DIR}/cardpulse.log"
}

info() {
    log "INFO" "${GREEN}$*${NC}"
}

warn() {
    log "WARN" "${YELLOW}$*${NC}"
}

error() {
    log "ERROR" "${RED}$*${NC}"
    exit 1
}

# 检查依赖
check_dependencies() {
    local deps=("curl" "date" "mkdir")
    
    # 检查 yaml 解析工具
    if command -v yq &> /dev/null; then
        YAML_PARSER="yq"
    elif command -v python3 &> /dev/null; then
        YAML_PARSER="python3"
    else
        error "需要安装 yq 或 python3 来解析 YAML 配置文件"
    fi
    
    for dep in "${deps[@]}"; do
        if ! command -v "$dep" &> /dev/null; then
            error "缺少依赖: $dep"
        fi
    done
}

# 读取配置
read_config() {
    local key="$1"
    local default="${2:-}"
    
    if [[ "$YAML_PARSER" == "yq" ]]; then
        local value=$(yq eval "$key" "$CONFIG_FILE" 2>/dev/null || echo "$default")
    else
        local value=$(python3 -c "
import yaml
import sys
try:
    with open('$CONFIG_FILE', 'r') as f:
        config = yaml.safe_load(f)
    keys = '$key'.split('.')
    result = config
    for k in keys:
        if isinstance(result, dict):
            result = result.get(k, '$default')
        else:
            result = '$default'
            break
    print(result if result is not None else '$default')
except:
    print('$default')
" 2>/dev/null)
    fi
    
    echo "$value"
}

# 初始化目录
init_dirs() {
    mkdir -p "$STATE_DIR" "$LOG_DIR"
}

# 检查是否需要发送
should_send() {
    local interval_days="$1"
    local last_success_file="${STATE_DIR}/last_success"
    
    if [[ ! -f "$last_success_file" ]]; then
        info "首次运行，需要发送保号短信"
        return 0
    fi
    
    local last_ts=$(cat "$last_success_file")
    local now_ts=$(date +%s)
    local diff_seconds=$((now_ts - last_ts))
    local diff_days=$((diff_seconds / 86400))
    
    if [[ "$diff_days" -lt "$interval_days" ]]; then
        info "距离上次发送仅 ${diff_days} 天，还需等待 $((interval_days - diff_days)) 天"
        return 1
    fi
    
    info "已达到 ${interval_days} 天间隔，需要发送保号短信"
    return 0
}

# 获取设备列表
get_devices() {
    local gateway_url="$1"
    local token="$2"
    local device_id="$3"
    
    local auth_header=""
    if [[ -n "$token" ]]; then
        auth_header="-H \"Authorization: Bearer $token\""
    fi
    
    if [[ -n "$device_id" ]]; then
        echo "$device_id"
        return
    fi
    
    # 获取所有设备
    local response
    if [[ -n "$token" ]]; then
        response=$(curl -s -H "Authorization: Bearer $token" "${gateway_url}/api/devices")
    else
        response=$(curl -s "${gateway_url}/api/devices")
    fi
    
    if [[ $? -ne 0 ]]; then
        error "无法连接到网关服务"
    fi
    
    # 解析设备 ID
    if [[ "$YAML_PARSER" == "python3" ]]; then
        echo "$response" | python3 -c "
import json, sys
data = json.load(sys.stdin)
if 'data' in data:
    for d in data['data']:
        print(d.get('id', ''))
" 2>/dev/null
    else
        echo "$response" | yq eval '.data[].id' - 2>/dev/null
    fi
}

# 发送短信
send_sms() {
    local gateway_url="$1"
    local token="$2"
    local device_id="$3"
    local phone="$4"
    local message="$5"
    
    local payload=$(cat <<EOF
{
    "device_id": "${device_id}",
    "phone": "${phone}",
    "message": "${message}"
}
EOF
)
    
    local response
    local http_code
    
    if [[ -n "$token" ]]; then
        response=$(curl -s -w "\n%{http_code}" -X POST \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer $token" \
            -d "$payload" \
            "${gateway_url}/api/sms/send")
    else
        response=$(curl -s -w "\n%{http_code}" -X POST \
            -H "Content-Type: application/json" \
            -d "$payload" \
            "${gateway_url}/api/sms/send")
    fi
    
    http_code=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | sed '$d')
    
    if [[ "$http_code" -ge 200 && "$http_code" -lt 300 ]]; then
        return 0
    else
        error "发送失败: HTTP $http_code - $body"
        return 1
    fi
}

# 记录成功状态
record_success() {
    local now_ts=$(date +%s)
    echo "$now_ts" > "${STATE_DIR}/last_success"
    info "发送成功，已更新状态文件"
}

# 发送通知
send_notification() {
    local success="$1"
    local device_id="$2"
    local phone="$3"
    local message="$4"
    
    local notify_enabled=$(read_config ".notify.enabled" "false")
    if [[ "$notify_enabled" != "true" ]]; then
        return
    fi
    
    local status_text="成功"
    if [[ "$success" != "true" ]]; then
        status_text="失败"
    fi
    
    local notify_msg="【CardPulse】保号短信${status_text}\n设备: ${device_id}\n号码: ${phone}\n时间: $(date '+%Y-%m-%d %H:%M:%S')"
    
    # Telegram 通知
    local tg_enabled=$(read_config ".notify.telegram.bot_token" "")
    if [[ -n "$tg_enabled" ]]; then
        local tg_token=$(read_config ".notify.telegram.bot_token")
        local tg_chat_id=$(read_config ".notify.telegram.chat_id")
        
        curl -s -X POST "https://api.telegram.org/bot${tg_token}/sendMessage" \
            -d "chat_id=${tg_chat_id}" \
            -d "text=${notify_msg}" \
            -d "parse_mode=HTML" > /dev/null 2>&1
    fi
    
    # Bark 通知
    local bark_enabled=$(read_config ".notify.bark.url" "")
    if [[ -n "$bark_enabled" ]]; then
        local bark_url=$(read_config ".notify.bark.url")
        
        curl -s "${bark_url}/CardPulse/${status_text}?sound=alert" \
            -d "body=${notify_msg}" > /dev/null 2>&1
    fi
}

# ============================================================================
# 主流程
# ============================================================================

main() {
    # 解析命令行参数
    local force_send=false
    local show_status=false
    
    while [[ $# -gt 0 ]]; do
        case $1 in
            -f|--force)
                force_send=true
                shift
                ;;
            -s|--status)
                show_status=true
                shift
                ;;
            -c|--config)
                CONFIG_FILE="$2"
                shift 2
                ;;
            -h|--help)
                echo "用法: cardpulse [选项]"
                echo ""
                echo "选项:"
                echo "  -f, --force      强制发送（忽略间隔检查）"
                echo "  -s, --status     显示状态信息"
                echo "  -c, --config     指定配置文件路径"
                echo "  -h, --help       显示帮助信息"
                exit 0
                ;;
            *)
                error "未知参数: $1"
                ;;
        esac
    done
    
    # 检查依赖
    check_dependencies
    
    # 初始化目录
    init_dirs
    
    # 检查配置文件
    if [[ ! -f "$CONFIG_FILE" ]]; then
        error "配置文件不存在: $CONFIG_FILE"
        echo "请复制配置文件示例: cp config/config.example.yaml $CONFIG_FILE"
        exit 1
    fi
    
    # 显示状态
    if [[ "$show_status" == "true" ]]; then
        echo "=== CardPulse 状态 ==="
        echo "配置文件: $CONFIG_FILE"
        
        if [[ -f "${STATE_DIR}/last_success" ]]; then
            local last_ts=$(cat "${STATE_DIR}/last_success")
            local last_date=$(date -d "@$last_ts" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date -r "$last_ts" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "未知")
            echo "上次发送: $last_date"
            
            local interval=$(read_config ".sms.interval_days" "$DEFAULT_INTERVAL_DAYS")
            local now_ts=$(date +%s)
            local diff_days=$(( (now_ts - last_ts) / 86400 ))
            local remaining=$((interval - diff_days))
            
            if [[ $remaining -gt 0 ]]; then
                echo "还需等待: ${remaining} 天"
            else
                echo "状态: 需要发送"
            fi
        else
            echo "上次发送: 从未发送"
            echo "状态: 需要发送"
        fi
        exit 0
    fi
    
    # 读取配置
    local gateway_url=$(read_config ".gateway.url" "http://localhost:7575")
    local gateway_token=$(read_config ".gateway.token" "")
    local device_id=$(read_config ".device.id" "")
    local phone=$(read_config ".sms.phone" "")
    local message=$(read_config ".sms.message" "$DEFAULT_MESSAGE")
    local interval_days=$(read_config ".sms.interval_days" "$DEFAULT_INTERVAL_DAYS")
    
    # 验证必要配置
    if [[ -z "$phone" ]]; then
        error "未配置短信接收号码 (sms.phone)"
    fi
    
    # 检查是否需要发送
    if [[ "$force_send" != "true" ]]; then
        if ! should_send "$interval_days"; then
            exit 0
        fi
    fi
    
    info "开始执行保号任务..."
    info "网关: $gateway_url"
    info "目标号码: $phone"
    info "间隔天数: $interval_days"
    
    # 获取设备列表
    local devices
    devices=$(get_devices "$gateway_url" "$gateway_token" "$device_id")
    
    if [[ -z "$devices" ]]; then
        error "未找到可用设备"
        send_notification "false" "无" "$phone" "$message"
        exit 1
    fi
    
    # 发送短信
    local success_count=0
    local fail_count=0
    
    for device in $devices; do
        [[ -z "$device" ]] && continue
        
        info "使用设备: $device 发送短信..."
        
        if send_sms "$gateway_url" "$gateway_token" "$device" "$phone" "$message"; then
            info "设备 $device 发送成功"
            ((success_count++))
        else
            error "设备 $device 发送失败"
            ((fail_count++))
        fi
    done
    
    # 更新状态
    if [[ $success_count -gt 0 ]]; then
        record_success
        send_notification "true" "$devices" "$phone" "$message"
        info "保号任务完成: 成功 $success_count, 失败 $fail_count"
    else
        send_notification "false" "$devices" "$phone" "$message"
        error "保号任务失败: 所有设备发送失败"
        exit 1
    fi
}

# 运行主函数
main "$@"
