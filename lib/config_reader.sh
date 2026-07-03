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
    elif command -v python3 &> /dev/null; then
        YAML_PARSER="python3"
    else
        echo "[ERROR] 需要安装 yq 或 python3 来解析 YAML 配置文件" >&2
        return 1
    fi
    
    # 检查配置文件是否存在
    if [[ ! -f "$CONFIG_FILE" ]]; then
        echo "[ERROR] 配置文件不存在: $CONFIG_FILE" >&2
        echo "请复制配置文件示例: cp config/config.example.yaml $CONFIG_FILE" >&2
        return 1
    fi
    
    return 0
}

# 读取配置项
# 用法: config_read ".serial.port" "/dev/ttyUSB0"
config_read() {
    local key="$1"
    local default="${2:-}"
    
    if [[ "$YAML_PARSER" == "yq" ]]; then
        local value=$(yq eval "$key" "$CONFIG_FILE" 2>/dev/null || echo "$default")
    else
        local value=$(CONFIG_PATH="$CONFIG_FILE" CONFIG_KEY="$key" CONFIG_DEFAULT="$default" python3 -c "
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
config_validate() {
    local errors=0
    
    # 检查串口配置
    local serial_port=$(config_read ".serial.port" "")
    local auto_detect=$(config_read ".serial.auto_detect" "true")
    
    if [[ -z "$serial_port" && "$auto_detect" != "true" ]]; then
        echo "[ERROR] 未配置串口设备 (serial.port)" >&2
        ((errors++))
    fi
    
    # 检查短信配置
    local phone=$(config_read ".sms.phone" "")
    if [[ -z "$phone" ]]; then
        echo "[ERROR] 未配置短信接收号码 (sms.phone)" >&2
        ((errors++))
    fi
    
    # 检查间隔配置
    local interval=$(config_read ".sms.interval_days" "179")
    if [[ ! "$interval" =~ ^[0-9]+$ ]] || [[ "$interval" -lt 1 ]]; then
        echo "[ERROR] 无效的间隔天数 (sms.interval_days): $interval" >&2
        ((errors++))
    fi
    
    return $errors
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
    echo "  phone: $(config_read '.sms.phone' '')"
    echo "  message: $(config_read '.sms.message' 'Hello from CardPulse')"
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
