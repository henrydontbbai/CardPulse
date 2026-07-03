# CardPulse 部署指南

本文档提供详细的 CardPulse 部署步骤，从零开始搭建完整的保号短信系统。

## 目录

1. [硬件准备](#1-硬件准备)
2. [系统安装](#2-系统安装)
3. [CardPulse 安装](#3-cardpulse-安装)
4. [配置说明](#4-配置说明)
5. [测试验证](#5-测试验证)
6. [定时任务](#6-定时任务)
7. [常见问题](#7-常见问题)

---

## 1. 硬件准备

### 推荐配置

| 设备 | 型号 | 说明 |
|------|------|------|
| 开发板 | 树莓派 4B/5 或 x86 工控机 | 2GB+ 内存，支持 Linux |
| 4G 模组 | Quectel EC20/EC25 或同等模组 | USB 供电 + 数据传输 |
| SIM 卡 | Google Voice 卡 | 待保号的卡 |
| 存储 | 16GB+ SD 卡/SSD | 系统 + 日志存储 |
| 电源 | 5V/3A 适配器 | 稳定供电，24 小时运行 |

### 硬件连接

```
┌─────────────────┐     USB      ┌─────────────────┐
│    开发板        │──────────────│    4G 模组      │
│  (树莓派/工控机)  │              │  (EC20/EC25)    │
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
    python3 \
    python3-yaml \
    minicom
```

---

## 3. CardPulse 安装

### 3.1 克隆仓库

```bash
git clone https://github.com/henrydontbbai/CardPulse.git
cd CardPulse
```

### 3.2 运行安装脚本

```bash
sudo ./scripts/install.sh
```

安装脚本会：
1. 检查并安装依赖
2. 复制文件到系统目录
3. 创建配置目录
4. 设置串口权限
5. 配置 cron 定时任务
6. 创建 systemd 服务

### 3.3 手动安装（可选）

```bash
# 复制可执行文件
sudo cp bin/cardpulse /usr/local/bin/
sudo chmod +x /usr/local/bin/cardpulse

# 复制库文件
sudo mkdir -p /opt/cardpulse/lib
sudo cp lib/*.sh /opt/cardpulse/lib/
sudo chmod +x /opt/cardpulse/lib/*.sh

# 创建配置目录
mkdir -p ~/.cardpulse/{state,logs}
cp config/config.example.yaml ~/.cardpulse/config.yaml
```

---

## 4. 配置说明

### 4.1 编辑配置文件

```bash
vim ~/.cardpulse/config.yaml
```

### 4.2 串口配置

```yaml
serial:
  # 串口设备路径
  port: "/dev/ttyUSB0"
  
  # 波特率（通常为 115200）
  baudrate: 115200
  
  # 自动检测（优先使用手动配置的端口）
  auto_detect: true
```

**如何找到串口设备：**

```bash
# 查看 USB 串口设备
ls /dev/ttyUSB* /dev/ttyACM*

# 查看设备信息
dmesg | grep tty
```

### 4.3 短信配置

```yaml
sms:
  # 接收号码（必须包含国家代码）
  phone: "+8613800138000"
  
  # 短信内容（支持中文）
  message: "Hello from CardPulse"
  
  # 保号间隔天数（GG 卡建议 179 天）
  interval_days: 179
  
  # 发送超时（秒）
  timeout: 30
```

### 4.4 通知配置（可选）

```yaml
notify:
  enabled: true
  
  # Telegram
  telegram:
    enabled: true
    bot_token: "123456789:ABCdefGHIjklMNOpqr"
    chat_id: "123456789"
  
  # 微信（Server酱）
  wechat:
    enabled: false
    send_key: ""
  
  # 其他通知渠道...
```

---

## 5. 测试验证

### 5.1 查看模组信息

```bash
cardpulse --info
```

输出示例：
```
=== 模组信息 ===
设备: /dev/ttyUSB0
波特率: 115200

厂商: Quectel
型号: EC25E
IMEI: 860000000000000
版本: ...

=== 状态信息 ===
SIM 卡: READY
信号强度: 18
网络状态: 1
运营商: CMCC
```

### 5.2 测试发送

```bash
cardpulse --test
```

### 5.3 查看状态

```bash
cardpulse --status
```

输出示例：
```
=== CardPulse 状态 ===

上次发送: 2026-07-04 12:00:00
距今: 0 天
状态: 需要发送

配置间隔: 179 天

=== 最近记录 ===
  2026-07-04 12:00:00 - success
```

### 5.4 手动测试 API

```bash
# 直接调用脚本
cardpulse --test

# 或手动执行
cardpulse --force
```

---

## 6. 定时任务

### 6.1 使用 cron（已自动配置）

安装脚本已自动添加 cron 任务：

```bash
# 查看 cron 任务
crontab -l

# 手动添加（如果需要）
crontab -e
# 添加：0 2 * * * /usr/local/bin/cardpulse >> ~/.cardpulse/logs/cardpulse.log 2>&1
```

### 6.2 使用 systemd timer（推荐）

```bash
# 启动定时器
sudo systemctl start cardpulse.timer

# 查看状态
sudo systemctl status cardpulse.timer

# 手动执行一次
sudo systemctl start cardpulse.service

# 查看日志
journalctl -u cardpulse.service
```

---

## 7. 常见问题

### Q1: 未找到串口设备

```bash
# 检查 USB 设备
lsusb

# 检查串口设备
ls /dev/ttyUSB*

# 查看内核日志
dmesg | tail -20
```

### Q2: 权限不足

```bash
# 将用户添加到 dialout 组
sudo usermod -aG dialout $USER

# 重新登录
logout
```

### Q3: 模组无响应

```bash
# 使用 minicom 手动测试
sudo minicom -D /dev/ttyUSB0 -b 115200

# 输入 AT 命令
AT
ATI
AT+CSQ
```

### Q4: 短信发送失败

```bash
# 查看详细日志
cardpulse --test

# 检查 SIM 卡状态
cardpulse --info

# 检查信号强度
cardpulse --info | grep "信号强度"
```

### Q5: 如何修改执行时间

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

### Q6: 如何查看历史记录

```bash
# 查看状态
cardpulse --status

# 查看日志
cat ~/.cardpulse/logs/cardpulse.log

# 查看历史记录
cat ~/.cardpulse/state/history.log
```

### Q7: 如何卸载

```bash
# 停止定时器
sudo systemctl stop cardpulse.timer
sudo systemctl disable cardpulse.timer

# 删除文件
sudo rm /usr/local/bin/cardpulse
sudo rm -rf /opt/cardpulse
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
- 保持设备 24 小时运行
- 定期检查日志确保正常运行

---

## 获取帮助

- GitHub Issues: https://github.com/henrydontbbai/CardPulse/issues
