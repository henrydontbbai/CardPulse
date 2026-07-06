#!/bin/bash
# ============================================================================
# CardPulse - 配置读取模块
# ============================================================================

# 防止重复加载
[[ -n "${_CONFIG_READER_LOADED:-}" ]] && return 0
_CONFIG_READER_LOADED=1

# 默认配置目录
CONFIG_DIR="${CARDPULSE_CONFIG_DIR:-$HOME/.cardpulse}"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"

# YAML 解析器
YAML_PARSER=""

# 初始化配置读取器
config_init() {
    # 检查 yaml 解析工具
    if command -v yq &> /dev/null; then
        YAML_PARSER="yq"
    elif command -v python3 &> /dev/null && python3 -c 'import yaml' >/dev/null 2>&1; then
        YAML_PARSER="python3"
    else
        echo "[ERROR] 需要安装 yq 或 python3-yaml 来解析 YAML 配置文件" >&2
        return 1
    fi

    # 检查配置文件是否存在
    if [[ ! -f "$CONFIG_FILE" ]]; then
        echo "[ERROR] 配置文件不存在: $CONFIG_FILE" >&2
        echo "请复制配置文件示例: cp config/config.example.yaml $CONFIG_FILE" >&2
        return 1
    fi

    if [[ -r "$CONFIG_FILE" ]]; then
        local perms
        perms=$(stat -c '%a' "$CONFIG_FILE" 2>/dev/null || echo "")
        if [[ -n "$perms" && $((8#$perms & 077)) -ne 0 ]]; then
            echo "[WARN] 配置文件权限过宽，建议执行: chmod 600 $CONFIG_FILE" >&2
        fi
    fi

    return 0
}

config_warn_required() {
    local label="$1"
    local key="$2"
    local value
    value=$(config_read "$key" "")
    if [[ -z "$value" ]]; then
        echo "[WARN] ${label} 已启用但未配置 ${key#*.notify.}" >&2
    fi
}

config_mask_string() {
    local s="$1"
    if [[ -z "$s" ]]; then
        echo ""
    elif [[ ${#s} -le 8 ]]; then
        echo "****"
    else
        local len=${#s}
        echo "${s:0:4}****${s:$((len - 4)):4}"
    fi
}

# 读取配置项
# 用法: config_read ".serial.port" "/dev/ttyUSB0"
config_read() {
    local key="$1"
    local default="${2:-}"
    local value

    if [[ "$YAML_PARSER" == "yq" ]]; then
        value=$(yq eval "$key" "$CONFIG_FILE" 2>/dev/null || echo "$default")
    else
        value=$(CONFIG_PATH="$CONFIG_FILE" CONFIG_KEY="$key" CONFIG_DEFAULT="$default" python3 -c "
import yaml
import sys
import os

config_path = os.environ['CONFIG_PATH']
key = os.environ['CONFIG_KEY']
default = os.environ['CONFIG_DEFAULT']

try:
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    keys = key.strip('.').split('.')
    result = config
    
    for k in keys:
        if isinstance(result, dict):
            result = result.get(k, default)
        else:
            result = default
            break
    
    print(result if result is not None else default)
except Exception:
    print(default)
" 2>/dev/null)
    fi
    
    echo "$value"
}

# 验证必要配置
config_is_true() {
    case "${1:-}" in
        true|True|TRUE|1|yes|Yes|YES|on|On|ON) return 0 ;;
        *) return 1 ;;
    esac
}

config_validate() {
    local errors=0

    # 检查串口配置
    local serial_port
    local auto_detect
    serial_port=$(config_read ".serial.port" "")
    auto_detect=$(config_read ".serial.auto_detect" "true")

    if [[ -z "$serial_port" ]] && ! config_is_true "$auto_detect"; then
        echo "[ERROR] 未配置串口设备 (serial.port)" >&2
        ((errors+=1))
    fi

    # 验证波特率
    local baudrate
    baudrate=$(config_read ".serial.baudrate" "115200")
    if [[ -n "$baudrate" ]]; then
        case "$baudrate" in
            9600|19200|38400|57600|115200|230400|460800|921600) ;;
            *)
                echo "[ERROR] 无效的波特率 (serial.baudrate): $baudrate (允许: 9600,19200,38400,57600,115200,230400,460800,921600)" >&2
                ((errors+=1))
                ;;
        esac
    fi

    # 检查短信配置
    local phone
    phone=$(config_read ".sms.phone" "")
    if [[ -z "$phone" ]]; then
        echo "[ERROR] 未配置短信接收号码 (sms.phone)" >&2
        ((errors+=1))
    fi

    # 验证号码格式：允许 +、数字和常见分隔符，清理后必须为 5-15 位号码
    if [[ -n "$phone" ]]; then
        local sanitized_phone="${phone//[[:space:].()-]/}"
        local digits_only="${sanitized_phone//[^0-9]/}"
        if [[ ! "$sanitized_phone" =~ ^\+?[0-9]{5,15}$ ]]; then
            echo "[ERROR] 无效的号码格式 (sms.phone): $phone" >&2
            ((errors+=1))
        fi
        if [[ ${#digits_only} -lt 5 || ${#digits_only} -gt 15 ]]; then
            echo "[ERROR] 无效的号码长度 (sms.phone): 数字位数为 ${#digits_only}（允许 5-15 位）" >&2
            ((errors+=1))
        fi
    fi

    # 检查间隔配置
    local interval
    interval=$(config_read ".sms.interval_days" "179")
    if [[ ! "$interval" =~ ^[0-9]+$ ]] || [[ "$interval" -lt 1 ]]; then
        echo "[ERROR] 无效的间隔天数 (sms.interval_days): $interval" >&2
        ((errors+=1))
    fi

    # 验证通知配置（启用但缺少必要字段时告警但不阻断）
    local notify_enabled
    notify_enabled=$(config_read ".notify.enabled" "false")
    if config_is_true "$notify_enabled"; then
        if config_is_true "$(config_read ".notify.telegram.enabled" "false")"; then
            config_warn_required "Telegram" ".notify.telegram.bot_token"
            config_warn_required "Telegram" ".notify.telegram.chat_id"
        fi
        if config_is_true "$(config_read ".notify.wechat.enabled" "false")"; then
            config_warn_required "微信" ".notify.wechat.send_key"
        fi
        if config_is_true "$(config_read ".notify.wecom.enabled" "false")"; then
            config_warn_required "企业微信" ".notify.wecom.webhook_url"
        fi
        if config_is_true "$(config_read ".notify.qq.enabled" "false")"; then
            config_warn_required "QQ" ".notify.qq.qmsg_key"
        fi
        if config_is_true "$(config_read ".notify.feishu.enabled" "false")"; then
            config_warn_required "飞书" ".notify.feishu.webhook_url"
        fi
        if config_is_true "$(config_read ".notify.dingtalk.enabled" "false")"; then
            config_warn_required "钉钉" ".notify.dingtalk.webhook_url"
        fi
        if config_is_true "$(config_read ".notify.bark.enabled" "false")"; then
            config_warn_required "Bark" ".notify.bark.url"
        fi
        if config_is_true "$(config_read ".notify.email.enabled" "false")"; then
            config_warn_required "Email" ".notify.email.smtp_host"
            config_warn_required "Email" ".notify.email.username"
            config_warn_required "Email" ".notify.email.password"
            config_warn_required "Email" ".notify.email.to"
        fi
    fi
    
    return "$errors"
}

# 获取所有配置（调试用）
config_dump() {
    echo "=== CardPulse 配置 ==="
    echo "配置文件: $CONFIG_FILE"
    echo ""
    echo "[串口配置]"
    echo "  port: $(config_read '.serial.port' '')"
    echo "  baudrate: $(config_read '.serial.baudrate' '115200')"
    echo "  auto_detect: $(config_read '.serial.auto_detect' 'true')"
    echo ""
    echo "[短信配置]"
    local sms_phone
    local sms_message
    sms_phone=$(config_read '.sms.phone' '')
    sms_message=$(config_read '.sms.message' 'Hello from CardPulse')
    echo "  phone: $(config_mask_string "$sms_phone")"
    echo "  message_length: ${#sms_message}"
    echo "  interval_days: $(config_read '.sms.interval_days' '179')"
    echo "  timeout: $(config_read '.sms.timeout' '30')"
    echo ""
    echo "[重试配置]"
    echo "  max_attempts: $(config_read '.retry.max_attempts' '3')"
    echo "  interval: $(config_read '.retry.interval' '10')"
    echo ""
    echo "[通知配置]"
    echo "  enabled: $(config_read '.notify.enabled' 'false')"
    echo "  telegram: $(config_read '.notify.telegram.enabled' 'false')"
    echo "  wechat: $(config_read '.notify.wechat.enabled' 'false')"
    echo "  wecom: $(config_read '.notify.wecom.enabled' 'false')"
    echo "  qq: $(config_read '.notify.qq.enabled' 'false')"
    echo "  feishu: $(config_read '.notify.feishu.enabled' 'false')"
    echo "  dingtalk: $(config_read '.notify.dingtalk.enabled' 'false')"
    echo "  bark: $(config_read '.notify.bark.enabled' 'false')"
    echo "  email: $(config_read '.notify.email.enabled' 'false')"
}
