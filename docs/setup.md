# CardPulse 部署指南

本文档提供详细的 CardPulse 部署步骤，从零开始搭建完整的保号短信系统。

## 目录

1. [硬件准备](#1-硬件准备)
2. [系统安装](#2-系统安装)
3. [VoHive 部署](#3-vohive-部署)
4. [CardPulse 安装](#4-cardpulse-安装)
5. [配置说明](#5-配置说明)
6. [测试验证](#6-测试验证)
7. [定时任务](#7-定时任务)
8. [常见问题](#8-常见问题)

---

## 1. 硬件准备

### 推荐配置

| 设备 | 型号 | 说明 |
|------|------|------|
| 开发板 | 树莓派 4B/5 或 x86 工控机 | 2GB+ 内存，支持 Linux |
| 4G 模组 | DJI Cell 模块 (基于 EC25) | USB 供电 + 数据传输 |
| SIM 卡 | Google Voice 卡 | 待保号的卡 |
| 存储 | 16GB+ SD 卡/SSD | 系统 + 日志存储 |
| 电源 | 5V/3A 适配器 | 稳定供电，24 小时运行 |

### 硬件连接

```
┌─────────────────┐     USB      ┌─────────────────┐
│    开发板        │──────────────│   大疆 4G 模组   │
│  (树莓派/工控机)  │              │   (DJI Cell)    │
└────────┬────────┘              └────────┬────────┘
         │                                │
         │ 以太网/WiFi                    │ 4G 网络
         │                                │
         ▼                                ▼
    ┌─────────┐                    ┌─────────────┐
    │  互联网  │                    │  移动网络    │
    └─────────┘                    └─────────────┘
```

---

## 2. 系统安装

### 2.1 安装 Raspberry Pi OS

```bash
# 下载 Raspberry Pi Imager
# https://www.raspberrypi.com/software/

# 选择 Raspberry Pi OS Lite (64-bit)
# 写入 SD 卡并启动
```

### 2.2 初始配置

```bash
# 更新系统
sudo apt update && sudo apt upgrade -y

# 设置时区
sudo timedatectl set-timezone Asia/Shanghai

# 启用 SSH
sudo systemctl enable ssh

# 配置网络（如果使用静态 IP）
sudo nmtui
```

### 2.3 安装必要工具

```bash
# 安装基础工具
sudo apt install -y \
    curl \
    wget \
    git \
    jq \
    unzip \
    htop \
    tmux
```

---

## 3. VoHive 部署

### 3.1 方式一：直接安装（推荐）

```bash
# 创建目录
mkdir -p ~/vohive/{config,data,logs}
cd ~/vohive

# 下载最新版本（根据你的架构选择）
# 树莓派 4B/5 (ARM64)
wget https://github.com/iniwex5/vohive/releases/latest/download/vohive_linux_arm64

# x86 工控机
# wget https://github.com/iniwex5/vohive/releases/latest/download/vohive_linux_amd64

# 添加执行权限
chmod +x vohive_linux_arm64

# 创建配置文件
cat > config/config.yaml << 'EOF'
server:
  port: 7575
  debug: false

web:
  username: admin
  password: 你的密码

devices: []

vowifi:
  enabled: false

webhook:
  enabled: false
EOF

# 启动服务
./vohive_linux_arm64 -c config/config.yaml
```

### 3.2 方式二：Docker 安装

```bash
# 安装 Docker
curl -fsSL https://get.docker.com | sh

# 添加当前用户到 docker 组
sudo usermod -aG docker $USER
newgrp docker

# 创建配置目录
mkdir -p ~/vohive/{config,data,logs}

# 创建 docker-compose.yml
cat > ~/vohive/docker-compose.yml << 'EOF'
services:
  vohive:
    image: iniwex/vohive:latest
    container_name: vohive
    restart: unless-stopped
    ports:
      - "7575:7575"
    volumes:
      - ./config:/app/config
      - ./data:/app/data
      - ./logs:/app/logs
    environment:
      - TZ=Asia/Shanghai
    privileged: true
    devices:
      - /dev/:/dev/
    network_mode: host
EOF

# 启动服务
cd ~/vohive
docker compose up -d
```

### 3.3 验证 VoHive

```bash
# 检查服务状态
curl http://localhost:7575/api/health

# 打开浏览器访问
# http://你的开发板IP:7575
# 默认账号: admin / admin123
```

### 3.4 配置 4G 模组

1. 登录 VoHive Web 界面
2. 进入「设备管理」
3. 系统会自动发现 USB 连接的 4G 模组
4. 点击设备进行配置：
   - 设置设备名称
   - 选择网络模式
   - 配置 APN（如果需要）

---

## 4. CardPulse 安装

### 4.1 一键安装

```bash
# 下载安装脚本
wget https://raw.githubusercontent.com/你的用户名/CardPulse/main/scripts/install.sh

# 添加执行权限
chmod +x install.sh

# 运行安装
sudo ./install.sh
```

### 4.2 手动安装

```bash
# 克隆仓库
git clone https://github.com/你的用户名/CardPulse.git
cd CardPulse

# 复制脚本
sudo cp scripts/keepalive.sh /usr/local/bin/cardpulse
sudo chmod +x /usr/local/bin/cardpulse

# 创建配置目录
mkdir -p ~/.cardpulse/{state,logs}

# 复制配置文件
cp config/config.example.yaml ~/.cardpulse/config.yaml
```

---

## 5. 配置说明

### 5.1 编辑配置文件

```bash
vim ~/.cardpulse/config.yaml
```

### 5.2 配置项说明

#### VoHive 连接配置

```yaml
vohive:
  url: "http://localhost:7575"  # VoHive 服务地址
  token: ""                      # API 认证 token（可选）
```

#### 设备配置

```yaml
device:
  id: ""  # 留空使用所有设备，或指定设备 ID
```

**如何获取设备 ID：**

1. 登录 VoHive Web 界面
2. 进入「设备管理」
3. 点击设备 → 查看详情
4. 复制设备 ID

#### 短信配置

```yaml
sms:
  phone: "+1234567890"      # 接收号码（必须包含国家代码）
  message: "Hello"           # 短信内容
  interval_days: 179         # 保号间隔天数
```

#### 通知配置（可选）

```yaml
notify:
  enabled: true
  telegram:
    bot_token: "123456:ABC-DEF..."
    chat_id: "123456789"
```

---

## 6. 测试验证

### 6.1 手动测试发送

```bash
# 使用 VoHive API 直接测试
curl -X POST http://localhost:7575/api/sms/send \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "你的设备ID",
    "phone": "+1234567890",
    "message": "测试短信"
  }'
```

### 6.2 测试 CardPulse

```bash
# 强制发送（忽略间隔检查）
cardpulse --force

# 查看状态
cardpulse --status

# 查看日志
tail -f ~/.cardpulse/logs/cardpulse.log
```

### 6.3 验证结果

```bash
# 检查状态文件
cat ~/.cardpulse/state/last_success

# 查看日志
tail -20 ~/.cardpulse/logs/cardpulse.log
```

---

## 7. 定时任务

### 7.1 使用 cron（简单）

```bash
# 编辑 crontab
crontab -e

# 添加以下行（每天凌晨 2 点执行）
0 2 * * * /usr/local/bin/cardpulse >> ~/.cardpulse/logs/cardpulse.log 2>&1
```

### 7.2 使用 systemd timer（推荐）

```bash
# 复制服务文件
sudo cp scripts/cardpulse.service /etc/systemd/system/
sudo cp scripts/cardpulse.timer /etc/systemd/system/

# 编辑服务文件中的用户路径
sudo sed -i 's|/root/.cardpulse|/home/你的用户名/.cardpulse|g' /etc/systemd/system/cardpulse.service

# 启用并启动定时器
sudo systemctl daemon-reload
sudo systemctl enable cardpulse.timer
sudo systemctl start cardpulse.timer

# 查看定时器状态
sudo systemctl status cardpulse.timer

# 手动执行一次测试
sudo systemctl start cardpulse.service
```

---

## 8. 常见问题

### Q1: 无法连接到 VoHive

```bash
# 检查 VoHive 是否运行
ps aux | grep vohive

# 检查端口是否监听
netstat -tlnp | grep 7575

# 检查防火墙
sudo ufw status
sudo ufw allow 7575/tcp
```

### Q2: 无法识别 4G 模组

```bash
# 检查 USB 设备
lsusb

# 检查串口设备
ls /dev/ttyUSB*

# 查看 dmesg 日志
dmesg | tail -20
```

### Q3: 短信发送失败

```bash
# 检查 VoHive 日志
tail -f ~/vohive/logs/app.log

# 手动测试 API
curl -X POST http://localhost:7575/api/sms/send \
  -H "Content-Type: application/json" \
  -d '{"device_id":"test","phone":"+1234567890","message":"test"}'
```

### Q4: 如何修改执行时间

**cron 方式：**
```bash
crontab -e
# 修改时间格式：分 时 日 月 周
# 例如每天下午 3 点执行：0 15 * * *
```

**systemd timer 方式：**
```bash
sudo vim /etc/systemd/system/cardpulse.timer
# 修改 OnCalendar 参数
sudo systemctl daemon-reload
sudo systemctl restart cardpulse.timer
```

### Q5: 如何查看历史执行记录

```bash
# 查看日志
cat ~/.cardpulse/logs/cardpulse.log

# 查看上次成功时间
date -d @$(cat ~/.cardpulse/state/last_success)
```

### Q6: 如何卸载

```bash
# 停止定时器
sudo systemctl stop cardpulse.timer
sudo systemctl disable cardpulse.timer

# 删除文件
sudo rm /usr/local/bin/cardpulse
sudo rm /etc/systemd/system/cardpulse.{service,timer}
sudo systemctl daemon-reload

# 删除配置（可选）
rm -rf ~/.cardpulse
```

---

## 附录：GG 卡保号规则

| 卡类型 | 保号周期 | 要求 |
|--------|----------|------|
| Google Voice | 180 天 | 发送/接听电话或短信 |
| Google Fi | 90 天 | 有网络活动 |
| Project Fi | 90 天 | 有网络活动 |

**建议：**
- 保号间隔设置为 179 天，留有余量
- 同时发送多条短信更保险
- 保持设备 24 小时运行

---

## 获取帮助

- GitHub Issues: https://github.com/你的用户名/CardPulse/issues
- VoHive 文档: https://github.com/iniwex5/vohive
