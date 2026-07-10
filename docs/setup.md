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
5. 配置定时任务（优先 systemd，必要时回退 cron）
6. 配置日志轮转

### 3.3 手动安装（可选）

```bash
# 复制可执行文件
sudo install -o root -g root -m 0755 bin/cardpulse /usr/local/bin/cardpulse

# 复制库文件
sudo install -d -o root -g root -m 0755 /opt/cardpulse/lib
sudo install -o root -g root -m 0755 lib/*.sh /opt/cardpulse/lib/
sudo install -o root -g root -m 0644 lib/pdu_encoder.py /opt/cardpulse/lib/pdu_encoder.py
sudo ln -sfn /opt/cardpulse/lib /usr/local/lib/cardpulse

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

**macOS 真机测试：**

macOS 可用于连接 4G 模组验证 AT 命令和真实短信发送，但不建议作为生产安装环境。先安装依赖：

```bash
brew install bash coreutils shellcheck python
python3 -m pip install pyyaml
```

查找串口：

```bash
ls /dev/cu.* /dev/tty.*
```

常见端口包括 `/dev/cu.usbserial*`、`/dev/cu.usbmodem*`、`/dev/cu.wchusbserial*`、`/dev/cu.SLAB_USBtoUART*`。建议在配置中指定端口并关闭自动检测：

```yaml
serial:
  port: "/dev/cu.usbserial-XXXX"
  baudrate: 115200
  auto_detect: false
```

macOS 下 `timeout` 由 Homebrew `coreutils` 提供，命令名为 `gtimeout`；CardPulse 会自动使用 `timeout` 或 `gtimeout`。

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

当前版本默认使用单条 PDU 短信，不做长短信拆分。GSM 7-bit 最多 160 septets，UCS2 最多 140 octets；超长内容会在发送前被拒绝。

### 4.4 通知配置（可选）

```yaml
notify:
  enabled: true
  
  # Telegram
  telegram:
    enabled: true
    bot_token: "YOUR_TELEGRAM_BOT_TOKEN"
    chat_id: "YOUR_TELEGRAM_CHAT_ID"
  
  # 微信（Server酱）
  wechat:
    enabled: false
    send_key: ""
  
  # 其他通知渠道...
```

---

## 5. 测试验证

### 5.1 安全硬件诊断

```bash
cardpulse --doctor
```

只有 `--doctor` 报告 `AT: OK` 后，才继续查看模组信息或测试发送。

### 5.2 查看模组信息

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

### 5.3 测试发送

```bash
# 真实短信测试：仅在确认授权后执行
# cardpulse --test
```

### 5.4 查看状态

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

### 5.5 开发者本地测试

```bash
sudo apt-get install -y python3-yaml shellcheck
bash tests/run.sh
shellcheck bin/cardpulse lib/*.sh scripts/install.sh
```

---

## 6. 定时任务

### 6.1 自动调度策略

安装脚本默认优先配置 systemd timer；如果 systemd 不可用，则回退到 cron。可用参数：

```bash
sudo ./scripts/install.sh --no-cron       # 仅使用 systemd，systemd 不可用时不配置调度器
sudo ./scripts/install.sh --no-systemd    # 禁用 systemd，使用 cron
sudo ./scripts/install.sh --with-cron     # systemd 可用时仍额外配置 cron
```

一般不建议同时启用 systemd 和 cron，除非你明确需要双调度兜底。

### 6.2 使用 cron

如果 systemd 不可用或使用 `--no-systemd`，安装脚本会添加 cron 任务：

```bash
# 查看 cron 任务
crontab -l

# 手动添加（如果需要，替换用户名和路径）
crontab -e
# 添加：0 2 * * * su -s /bin/sh -c 'CARDPULSE_CONFIG_DIR=/home/YOUR_USER/.cardpulse CARDPULSE_LIB_DIR=/opt/cardpulse/lib /usr/local/bin/cardpulse >> /home/YOUR_USER/.cardpulse/logs/cardpulse.log 2>&1' YOUR_USER
```

### 6.3 使用 systemd timer（推荐）

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

## 短信测试前的硬件诊断

CardPulse 需要 AT 串口。操作系统能列出 USB 设备还不够。

安全顺序：

```bash
cardpulse --doctor
cardpulse --info
# cardpulse --test only after you intentionally approve a real SMS send
```

`cardpulse --doctor` 不会发送短信，也不会写入 CardPulse 状态。如果存在 AT 串口，它可能会发送 AT 查询命令。如果结果是 `No AT serial port found`，先不要运行 `--info` 或 `--test`。

在 macOS 上，先找 `/dev/cu.usbserial*`、`/dev/cu.usbmodem*`、`/dev/cu.wchusbserial*` 或 `/dev/cu.SLAB_USBtoUART*`。DJI/Baiwang `2CA3:4006` 如果只出现在 USB 列表里，只能说明系统看到了设备；必须同时出现 AT 串口并对 `AT` 返回 `OK`，CardPulse 才能使用。

如果要测试 VoHive/MBIM/QMI 路线，建议在 Apple Silicon Mac 上使用 Ubuntu ARM64 虚拟机并做 USB 直通；先只做 `/dev/cdc-wdm*`、`/dev/wwan*` 和驱动绑定检查，不要直接发短信。

当前已经验证通过的日常运维主线路径是 **Windows 控制机 + WSL2 + usbipd + CardPulse Web**。这条路径的一键恢复脚本、Web 首页、消息中心和恢复状态文件说明统一见 [docs/web-control.md](docs/web-control.md)。

硬件诊断细节见 `docs/hardware-diagnostics.md`。
