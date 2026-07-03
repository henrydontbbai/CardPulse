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
REPO_URL="https://github.com/cardpulse/cardpulse.git"
INSTALL_DIR="/usr/local/bin"
CONFIG_DIR="$HOME/.cardpulse"

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
            error "不支持的架构: $arch"
            ;;
    esac
}

# 安装依赖
install_deps() {
    info "安装依赖..."
    
    if command -v apt-get &> /dev/null; then
        apt-get update
        apt-get install -y curl jq
    elif command -v yum &> /dev/null; then
        yum install -y curl jq
    elif command -v dnf &> /dev/null; then
        dnf install -y curl jq
    elif command -v pacman &> /dev/null; then
        pacman -S --noconfirm curl jq
    else
        warn "无法自动安装依赖，请手动安装: curl, jq"
    fi
}

# 下载并安装 CardPulse
install_cardpulse() {
    info "安装 CardPulse..."
    
    # 创建临时目录
    local tmp_dir=$(mktemp -d)
    trap "rm -rf $tmp_dir" EXIT
    
    # 下载仓库
    info "下载 CardPulse..."
    git clone --depth 1 "$REPO_URL" "$tmp_dir"
    
    # 安装脚本
    cp "$tmp_dir/scripts/keepalive.sh" "$INSTALL_DIR/cardpulse"
    chmod +x "$INSTALL_DIR/cardpulse"
    
    info "CardPulse 已安装到 $INSTALL_DIR/cardpulse"
}

# 创建配置目录
setup_config() {
    info "设置配置目录..."
    
    mkdir -p "$CONFIG_DIR"
    
    if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
        info "创建配置文件..."
        cat > "$CONFIG_DIR/config.yaml" << 'EOF'
# CardPulse 配置文件
# 详细配置说明请参考 README.md

# 4G 模组管理服务配置
gateway:
  # 管理服务地址
  url: "http://localhost:7575"
  # API 认证 token（如果启用了认证）
  token: ""

device:
  # 设备 ID（留空则自动使用所有设备）
  # 在管理后台 → 设备管理 → 查看设备详情
  id: ""

sms:
  # 接收短信的手机号码
  # GG 卡请使用 Google Voice 注册的号码
  phone: "+1234567890"
  
  # 短信内容（任意内容即可）
  message: "Hello from CardPulse"
  
  # 保号间隔天数（GG 卡建议 179 天）
  interval_days: 179

# 通知配置（可选）
notify:
  enabled: false
  
  # Telegram 通知
  # telegram:
  #   bot_token: "your_bot_token"
  #   chat_id: "your_chat_id"
  
  # Bark 通知（iOS）
  # bark:
  #   url: "https://api.day.app/your_key"
EOF
        
        info "配置文件已创建: $CONFIG_DIR/config.yaml"
        warn "请编辑配置文件填入你的信息！"
    else
        info "配置文件已存在，跳过创建"
    fi
    
    # 创建状态和日志目录
    mkdir -p "$CONFIG_DIR/state" "$CONFIG_DIR/logs"
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
    echo "  1. 编辑配置文件: vim $CONFIG_DIR/config.yaml"
    echo "  2. 测试运行: sudo cardpulse"
    echo "  3. 查看日志: tail -f $CONFIG_DIR/logs/cardpulse.log"
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
    setup_cron
    setup_systemd
    show_info
}

main "$@"
