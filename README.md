# CardPulse / 卡脉

> 让每一张 SIM 卡都有跳动的脉搏

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Shell](https://img.shields.io/badge/Shell-Bash-orange)](scripts/keepalive.sh)

## 简介

CardPulse 是一个轻量级的 SIM 卡保活工具，通过定时发送短信实现号码保活功能。

专为 **GG 卡 (Google Voice)** 等需要定期发送短信以防止号码回收的场景设计。

## 功能特性

- **定时保号**：按设定天数自动发送短信，保持 SIM 卡活跃
- **智能调度**：仅在达到设定天数时发送，避免浪费
- **状态持久化**：记录上次发送时间，重启后继续计时
- **日志完善**：所有操作记录到日志文件，便于排查
- **多设备支持**：可同时管理多张 SIM 卡
- **消息通知**：支持 Telegram/Bark 等推送保号结果

## 快速开始

### 1. 安装 CardPulse

```bash
# 一键安装
curl -fsSL https://raw.githubusercontent.com/cardpulse/cardpulse/main/scripts/install.sh | bash
```

或手动安装：

```bash
# 克隆仓库
git clone https://github.com/cardpulse/cardpulse.git
cd CardPulse

# 复制配置文件
cp config/config.example.yaml ~/.cardpulse/config.yaml

# 编辑配置
vim ~/.cardpulse/config.yaml

# 安装到系统
sudo cp scripts/keepalive.sh /usr/local/bin/cardpulse
sudo chmod +x /usr/local/bin/cardpulse
```

### 2. 配置

编辑 `~/.cardpulse/config.yaml`：

```yaml
# 4G 模组管理服务配置
gateway:
  url: "http://localhost:7575"
  token: ""  # 可选，如果启用了 API 认证

device:
  id: ""  # 留空使用所有设备，或指定设备 ID

sms:
  phone: "+1234567890"  # GG 卡号码
  message: "Hello from CardPulse"
  interval_days: 179

notify:
  enabled: false
  telegram:
    bot_token: ""
    chat_id: ""
```

### 3. 设置定时任务

```bash
# 添加 cron 任务（每天凌晨 2 点检查）
echo "0 2 * * * /usr/local/bin/cardpulse" | crontab -

# 或使用 systemd timer（推荐）
sudo cp scripts/cardpulse.service /etc/systemd/system/
sudo cp scripts/cardpulse.timer /etc/systemd/system/
sudo systemctl enable --now cardpulse.timer
```

### 4. 验证

```bash
# 手动执行一次
cardpulse

# 查看日志
tail -f ~/.cardpulse/logs/cardpulse.log

# 查看上次发送时间
cat ~/.cardpulse/state/last_success
```

## 项目结构

```
CardPulse/
├── README.md                    # 项目说明
├── LICENSE                      # MIT 许可证
├── .gitignore
├── scripts/
│   ├── keepalive.sh             # 核心保号脚本
│   ├── install.sh               # 一键安装脚本
│   ├── cardpulse.service        # systemd 服务
│   └── cardpulse.timer          # systemd 定时器
├── config/
│   └── config.example.yaml      # 配置文件示例
└── docs/
    └── setup.md                 # 详细部署指南
```

## 常见问题

### Q: 如何查看设备 ID？

登录 4G 模组管理后台 → 设备管理 → 点击设备 → 查看设备详情中的 ID。

### Q: GG 卡保号周期是多少天？

Google Voice 要求每 **179 天**至少有一次活动（发送/接听电话或短信）。

### Q: 可以发送到任意号码吗？

是的，短信内容和接收号码可以是任意的，只要能成功发送即可。

### Q: 如何同时保多张卡？

在配置文件中添加多个设备配置，或创建多个配置文件分别运行。

## License

[MIT](LICENSE)
