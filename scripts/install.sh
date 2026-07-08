#!/bin/bash
# ============================================================================
# CardPulse 安装脚本
# ============================================================================

set -euo pipefail

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 配置
INSTALL_DIR="/usr/local/bin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TARGET_USER="${SUDO_USER:-${USER:-root}}"
TARGET_HOME="$(getent passwd "$TARGET_USER" 2>/dev/null | cut -d: -f6 || true)"
[[ -z "$TARGET_HOME" ]] && TARGET_HOME="$HOME"
CONFIG_DIR="${CARDPULSE_CONFIG_DIR:-$TARGET_HOME/.cardpulse}"
LIB_DIR="/opt/cardpulse/lib"
WANT_SYSTEMD=true
WANT_CRON=auto
SCHEDULER_SUMMARY="未配置"

info() {
    echo -e "${GREEN}[INFO]${NC} $*"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

error() {
    echo -e "${RED}[ERROR]${NC} $*"
    exit 1
}

shell_quote() {
    local value="$1"
    printf "'%s'" "${value//\'/\'\\\'\'}"
}

validate_generated_value() {
    local label="$1"
    local value="$2"

    case "$value" in
        *$'\n'*|*$'\r'*|*\%*|*\"*|*\'*)
            error "$label 包含不支持的字符（换行、引号或 %）: $value"
            ;;
    esac
}

validate_install_inputs() {
    validate_generated_value "TARGET_USER" "$TARGET_USER"
    validate_generated_value "CONFIG_DIR" "$CONFIG_DIR"
    validate_generated_value "LIB_DIR" "$LIB_DIR"
    validate_generated_value "INSTALL_DIR" "$INSTALL_DIR"
}

show_usage() {
    echo "用法: sudo ./scripts/install.sh [选项]"
    echo ""
    echo "选项:"
    echo "  --no-systemd    不配置 systemd timer"
    echo "  --no-cron       不配置 cron fallback"
    echo "  --with-cron     即使 systemd 可用也额外配置 cron"
    echo "  -h, --help      显示帮助"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --no-systemd)
                WANT_SYSTEMD=false
                shift
                ;;
            --no-cron)
                WANT_CRON=false
                shift
                ;;
            --with-cron)
                WANT_CRON=true
                shift
                ;;
            -h|--help)
                show_usage
                exit 0
                ;;
            *)
                error "未知参数: $1"
                ;;
        esac
    done
}

# 检查是否为 root 用户
check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "请使用 root 用户运行此脚本 (sudo ./install.sh)"
    fi
}

# 检查系统
check_system() {
    info "检查系统环境..."

    # 检查操作系统
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        info "检测到系统: ${ID:-unknown} ${VERSION_ID:-}"
    else
        warn "无法检测操作系统类型"
    fi

    # 检查架构
    local arch
    arch=$(uname -m)
    info "系统架构: $arch"
    
    case $arch in
        x86_64|amd64|aarch64|arm64|armv7l|armhf)
            ;;
        *)
            warn "未知架构: $arch，继续安装"
            ;;
    esac
}

# 安装依赖
install_deps() {
    info "检查依赖..."
    
    # 检查必要工具
    local deps=("curl" "stty" "timeout" "flock" "python3")
    local missing=()
    
    for dep in "${deps[@]}"; do
        if ! command -v "$dep" &> /dev/null; then
            missing+=("$dep")
        fi
    done
    
    # 检查 YAML 解析工具
    if ! command -v yq &> /dev/null && ! python3 -c 'import yaml' >/dev/null 2>&1; then
        missing+=("yq 或 python3-yaml")
    fi
    
    if [[ ${#missing[@]} -gt 0 ]]; then
        warn "缺少依赖: ${missing[*]}"
        info "尝试安装依赖..."
        
        if command -v apt-get &> /dev/null; then
            apt-get update
            apt-get install -y curl python3 python3-yaml coreutils util-linux
        elif command -v yum &> /dev/null; then
            yum install -y curl python3 python3-pyyaml coreutils util-linux
        elif command -v dnf &> /dev/null; then
            dnf install -y curl python3 python3-pyyaml coreutils util-linux
        elif command -v pacman &> /dev/null; then
            pacman -S --noconfirm curl python python-yaml coreutils util-linux
        else
            warn "无法自动安装依赖，请手动安装: curl, python3, python3-yaml"
        fi
    fi
    
    info "✓ 依赖检查完成"
}

# 安装 CardPulse
install_cardpulse() {
    info "安装 CardPulse..."
    
    # 创建安装目录
    install -d -o root -g root -m 0755 "$INSTALL_DIR" "$LIB_DIR"
    
    # 复制文件
    install -o root -g root -m 0755 "$PROJECT_DIR/bin/cardpulse" "$INSTALL_DIR/cardpulse"
    
    # 复制库文件
    install -o root -g root -m 0755 "$PROJECT_DIR/lib/"*.sh "$LIB_DIR/"
    # 复制 PDU 编码器
    if [[ -f "$PROJECT_DIR/lib/pdu_encoder.py" ]]; then
        install -o root -g root -m 0644 "$PROJECT_DIR/lib/pdu_encoder.py" "$LIB_DIR/pdu_encoder.py"
    fi
    if [[ -f "$PROJECT_DIR/lib/pdu_decoder.py" ]]; then
        install -o root -g root -m 0644 "$PROJECT_DIR/lib/pdu_decoder.py" "$LIB_DIR/pdu_decoder.py"
    fi
    
    # 创建符号链接
    ln -sf "$LIB_DIR" /usr/local/lib/cardpulse
    
    info "CardPulse 已安装到 $INSTALL_DIR/cardpulse"
    info "库文件已安装到 /opt/cardpulse/lib/"
}

# 创建配置目录
setup_config() {
    info "设置配置目录..."
    
    mkdir -p "$CONFIG_DIR" "$CONFIG_DIR/state" "$CONFIG_DIR/logs"
    
    if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
        info "创建配置文件..."
        cp "$PROJECT_DIR/config/config.example.yaml" "$CONFIG_DIR/config.yaml"
        
        info "配置文件已创建: $CONFIG_DIR/config.yaml"
        warn "请编辑配置文件填入你的信息！"
    else
        info "配置文件已存在，跳过创建"
    fi

    local target_group
    target_group=$(id -gn "$TARGET_USER" 2>/dev/null || true)
    if [[ -n "$target_group" ]]; then
        chown -R "$TARGET_USER:$target_group" "$CONFIG_DIR"
    else
        chown -R "$TARGET_USER" "$CONFIG_DIR"
    fi
    chmod 700 "$CONFIG_DIR" "$CONFIG_DIR/state" "$CONFIG_DIR/logs"
    chmod 600 "$CONFIG_DIR/config.yaml" 2>/dev/null || true
}

# 设置串口权限
setup_permissions() {
    info "设置串口权限..."
    
    # 将当前用户添加到 dialout 组
    local current_user="$TARGET_USER"
    
    if ! getent group dialout >/dev/null 2>&1; then
        warn "系统不存在 dialout 组，跳过自动串口权限设置"
        warn "请根据发行版手动授予 $current_user 访问串口设备的权限"
        return
    fi

    if id -nG "$current_user" | grep -q "dialout"; then
        info "用户 $current_user 已在 dialout 组中"
    else
        usermod -aG dialout "$current_user"
        info "已将用户 $current_user 添加到 dialout 组"
        warn "需要重新登录才能生效"
    fi
}

# 设置定时任务
setup_cron() {
    info "设置定时任务..."
    
    # 检查是否已存在 cron 任务
    if crontab -l 2>/dev/null | grep -q "cardpulse"; then
        warn "已存在 CardPulse cron 任务，跳过"
        return
    fi
    
    # 添加 cron 任务（每天凌晨 2 点执行）
    local cron_command
    local cron_line
    cron_command="CARDPULSE_CONFIG_DIR=$(shell_quote "$CONFIG_DIR") CARDPULSE_LIB_DIR=$(shell_quote "$LIB_DIR") $(shell_quote "$INSTALL_DIR/cardpulse") >> $(shell_quote "$CONFIG_DIR/logs/cardpulse.log") 2>&1"
    cron_line="0 2 * * * su -s /bin/sh -c $(shell_quote "$cron_command") $(shell_quote "$TARGET_USER")"
    (crontab -l 2>/dev/null; echo "$cron_line") | crontab -
    
    info "已添加 cron 任务：每天凌晨 2 点执行保号检查"
}

systemd_usable() {
    [[ -d /etc/systemd/system ]] || return 1
    command -v systemctl >/dev/null 2>&1 || return 1
    [[ -d /run/systemd/system ]] || return 1
}

# 创建 systemd 服务（可选）
setup_systemd() {
    if ! systemd_usable; then
        info "系统不支持 systemd，跳过服务创建"
        return 1
    fi
    
    info "创建 systemd 服务..."
    
    cat > /etc/systemd/system/cardpulse.service << EOF
[Unit]
Description=CardPulse - SIM卡定时保号工具
After=network.target

[Service]
Type=oneshot
User=$TARGET_USER
Environment="CARDPULSE_CONFIG_DIR=$CONFIG_DIR"
Environment="CARDPULSE_LIB_DIR=$LIB_DIR"
ExecStart=$INSTALL_DIR/cardpulse
WorkingDirectory="$CONFIG_DIR"

[Install]
WantedBy=multi-user.target
EOF
    
    cat > /etc/systemd/system/cardpulse.timer << EOF
[Unit]
Description=CardPulse 定时器

[Timer]
OnCalendar=*-*-* 02:00:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
EOF
    
    systemctl daemon-reload
    systemctl enable cardpulse.timer
    
    info "已创建 systemd 定时器"
    info "使用以下命令管理："
    echo "  启动定时器: sudo systemctl start cardpulse.timer"
    echo "  查看状态:   sudo systemctl status cardpulse.timer"
    echo "  手动执行:   sudo systemctl start cardpulse.service"
}

setup_scheduler() {
    local systemd_ok=false
    if systemd_usable; then
        systemd_ok=true
    fi

    if [[ "$WANT_SYSTEMD" == "true" && "$systemd_ok" == "true" ]]; then
        setup_systemd
        SCHEDULER_SUMMARY="systemd timer"
        if [[ "$WANT_CRON" == "true" ]]; then
            setup_cron
            SCHEDULER_SUMMARY="systemd timer + cron"
        fi
        return 0
    fi

    if [[ "$WANT_SYSTEMD" == "true" && "$systemd_ok" != "true" ]]; then
        warn "systemd 不可用，将尝试 cron fallback"
    fi

    if [[ "$WANT_CRON" != "false" ]]; then
        setup_cron
        SCHEDULER_SUMMARY="cron"
        return 0
    fi

    warn "未配置自动调度器；请手动运行 cardpulse 或自行配置定时任务"
    SCHEDULER_SUMMARY="未配置"
}

# 配置日志轮转
setup_logrotate() {
    if [[ ! -f /etc/logrotate.conf ]]; then
        info "未检测到 logrotate，跳过日志轮转配置"
        return
    fi

    local logrotate_conf="/etc/logrotate.d/cardpulse"
    if [[ -f "$logrotate_conf" ]]; then
        info "日志轮转配置已存在，跳过"
        return
    fi

    info "配置日志轮转..."
    cat > "$logrotate_conf" << EOF
"$CONFIG_DIR/logs/cardpulse.log" {
    monthly
    rotate 12
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
    info "✓ 日志轮转配置已创建: $logrotate_conf"
}

# 显示安装信息
show_info() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${GREEN}CardPulse 安装完成！${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""
    echo "配置文件: $CONFIG_DIR/config.yaml"
    echo "日志文件: $CONFIG_DIR/logs/cardpulse.log"
    echo "状态目录: $CONFIG_DIR/state/"
    echo ""
    echo "下一步："
    echo "  1. 编辑配置文件: sudo vim $CONFIG_DIR/config.yaml"
    echo "  2. 查看模组信息: cardpulse --info"
    echo "  3. 测试发送: cardpulse --test"
    echo "  4. 查看状态: cardpulse --status"
    echo ""
    echo "定时任务: $SCHEDULER_SUMMARY"
    if [[ "$SCHEDULER_SUMMARY" != "未配置" ]]; then
        echo "定时任务已配置，将在每天凌晨 2 点自动执行。"
    fi
    echo ""
    echo "如需帮助，请查看 README.md"
    echo ""
}

# 主函数
main() {
    echo -e "${BLUE}CardPulse 安装程序${NC}"
    echo ""

    parse_args "$@"
    check_root
    check_system
    validate_install_inputs
    install_deps
    install_cardpulse
    setup_config
    setup_permissions
    setup_scheduler
    setup_logrotate
    show_info
}

main "$@"
