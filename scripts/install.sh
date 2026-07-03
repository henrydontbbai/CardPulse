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
REPO_URL="https://github.com/henrydontbbai/CardPulse.git"
INSTALL_DIR="/usr/local/bin"
CONFIG_DIR="$HOME/.cardpulse"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

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
        info "检测到系统: $ID $VERSION_ID"
    else
        warn "无法检测操作系统类型"
    fi
    
    # 检查架构
    local arch=$(uname -m)
    info "系统架构: $arch"
    
    case $arch in
        x86_64|amd64)
            ARCH="amd64"
            ;;
        aarch64|arm64)
            ARCH="arm64"
            ;;
        armv7l|armhf)
            ARCH="armv7"
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
    local deps=("curl" "stty")
    local missing=()
    
    for dep in "${deps[@]}"; do
        if ! command -v "$dep" &> /dev/null; then
            missing+=("$dep")
        fi
    done
    
    # 检查 YAML 解析工具
    if ! command -v yq &> /dev/null && ! command -v python3 &> /dev/null; then
        missing+=("yq 或 python3")
    fi
    
    if [[ ${#missing[@]} -gt 0 ]]; then
        warn "缺少依赖: ${missing[*]}"
        info "尝试安装依赖..."
        
        if command -v apt-get &> /dev/null; then
            apt-get update
            apt-get install -y curl python3 python3-yaml
        elif command -v yum &> /dev/null; then
            yum install -y curl python3 python3-pyyaml
        elif command -v dnf &> /dev/null; then
            dnf install -y curl python3 python3-pyyaml
        elif command -v pacman &> /dev/null; then
            pacman -S --noconfirm curl python python-yaml
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
    mkdir -p "$INSTALL_DIR"
    
    # 复制文件
    cp "$PROJECT_DIR/bin/cardpulse" "$INSTALL_DIR/cardpulse"
    chmod +x "$INSTALL_DIR/cardpulse"
    
    # 复制库文件
    mkdir -p "/opt/cardpulse/lib"
    cp "$PROJECT_DIR/lib/"*.sh "/opt/cardpulse/lib/"
    chmod +x /opt/cardpulse/lib/*.sh
    
    # 创建符号链接
    ln -sf /opt/cardpulse/lib /usr/local/lib/cardpulse
    
    info "CardPulse 已安装到 $INSTALL_DIR/cardpulse"
    info "库文件已安装到 /opt/cardpulse/lib/"
}

# 创建配置目录
setup_config() {
    info "设置配置目录..."
    
    mkdir -p "$CONFIG_DIR"
    mkdir -p "$CONFIG_DIR/state"
    mkdir -p "$CONFIG_DIR/logs"
    
    if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
        info "创建配置文件..."
        cp "$PROJECT_DIR/config/config.example.yaml" "$CONFIG_DIR/config.yaml"
        
        info "配置文件已创建: $CONFIG_DIR/config.yaml"
        warn "请编辑配置文件填入你的信息！"
    else
        info "配置文件已存在，跳过创建"
    fi
}

# 设置串口权限
setup_permissions() {
    info "设置串口权限..."
    
    # 将当前用户添加到 dialout 组
    local current_user="${SUDO_USER:-$USER}"
    
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
    (crontab -l 2>/dev/null; echo "0 2 * * * $INSTALL_DIR/cardpulse >> $CONFIG_DIR/logs/cardpulse.log 2>&1") | crontab -
    
    info "已添加 cron 任务：每天凌晨 2 点执行保号检查"
}

# 创建 systemd 服务（可选）
setup_systemd() {
    if [[ ! -d /etc/systemd/system ]]; then
        info "系统不支持 systemd，跳过服务创建"
        return
    fi
    
    info "创建 systemd 服务..."
    
    cat > /etc/systemd/system/cardpulse.service << EOF
[Unit]
Description=CardPulse - SIM卡定时保号工具
After=network.target

[Service]
Type=oneshot
User=$SUDO_USER
ExecStart=$INSTALL_DIR/cardpulse
WorkingDirectory=$CONFIG_DIR

[Install]
WantedBy=multi-user.target
EOF
    
    cat > /etc/systemd/system/cardpulse.timer << EOF
[Unit]
Description=CardPulse 定时器

[Timer]
OnCalendar=*-*-* 02:00:00
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
    echo "定时任务已配置，将在每天凌晨 2 点自动执行。"
    echo ""
    echo "如需帮助，请查看 README.md"
    echo ""
}

# 主函数
main() {
    echo -e "${BLUE}CardPulse 安装程序${NC}"
    echo ""
    
    check_root
    check_system
    install_deps
    install_cardpulse
    setup_config
    setup_permissions
    setup_cron
    setup_systemd
    show_info
}

main "$@"
