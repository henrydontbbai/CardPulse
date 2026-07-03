# CardPulse / 卡脉

> 让每一张 SIM 卡都有跳动的脉搏

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Shell](https://img.shields.io/badge/Shell-Bash-orange)](bin/cardpulse)

## 简介

CardPulse 是一个轻量级的 SIM 卡保活工具，通过 AT 命令直接控制 4G 模组发送定时短信，实现号码保活功能。

专为 **GG 卡 (Google Voice)** 等需要定期发送短信以防止号码回收的场景设计。

## 功能特性

- **完全独立**：无需任何外部服务，直接通过串口控制 4G 模组
- **TEXT + PDU 模式**：支持英文和中文短信
- **定时保号**：按设定天数自动发送短信，保持 SIM 卡活跃
- **智能调度**：仅在达到设定天数时发送，避免浪费
- **状态持久化**：记录上次发送时间，重启后继续计时
- **多渠道通知**：支持 Telegram、微信、企业微信、QQ、飞书、钉钉、Bark、Email
- **自动检测**：自动发现串口设备，支持手动配置
- **完善日志**：所有操作记录到日志文件，便于排查

## 硬件要求

| 设备 | 说明 |
|------|------|
| 开发板 | 树莓派 4B/5、x86 工控机或其他 Linux 设备 |
| 4G 模组 | Quectel EC20/EC25、华为 ME909 等支持 AT 命令的模组 |
| SIM 卡 | 待保号的 Google Voice 卡或其他 SIM 卡 |
| 电源 | 5V/3A 适配器，24 小时运行 |

## 快速开始

### 1. 安装 CardPulse

```bash
# 克隆仓库
git clone https://github.com/henrydontbbai/CardPulse.git
cd CardPulse

# 运行安装脚本
sudo ./scripts/install.sh
```

### 2. 配置

编辑配置文件 `~/.cardpulse/config.yaml`：

```yaml
# 串口配置
serial:
  port: "/dev/ttyUSB0"
  baudrate: 115200
  auto_detect: true

# 短信配置
sms:
  phone: "+8613800138000"  # 你的 GG 卡号码
  message: "Hello from CardPulse"
  interval_days: 179
```

### 3. 验证

```bash
# 查看模组信息
cardpulse --info

# 测试发送
cardpulse --test

# 查看状态
cardpulse --status
```

### 4. 设置定时任务

安装脚本已自动配置 cron 任务（每天凌晨 2 点执行）。

也可以使用 systemd timer：

```bash
sudo systemctl start cardpulse.timer
sudo systemctl status cardpulse.timer
```

## 命令行选项

```
用法: cardpulse [选项]

选项:
  -c, --config FILE    指定配置文件路径
  -f, --force          强制发送（忽略间隔检查）
  -s, --status         显示状态信息
  -t, --test           测试发送（不记录状态）
  -i, --info           显示模组信息
  -r, --reset          重置状态（重新开始计时）
  -n, --notify         测试通知功能
  -v, --version        显示版本
  -h, --help           显示帮助
```

## 通知配置

CardPulse 支持 7 种主流通讯软件通知，在配置文件中启用并填入相应信息即可：

| 通讯软件 | 配置项 | 获取方式 |
|----------|--------|----------|
| Telegram | `notify.telegram` | [创建 Bot](https://core.telegram.org/bots) |
| 微信 | `notify.wechat` | [Server酱](https://sct.ftqq.com/) |
| 企业微信 | `notify.wecom` | 群机器人 Webhook |
| QQ | `notify.qq` | [Qmsg](https://qmsg.zendee.cn/) |
| 飞书 | `notify.feishu` | 群自定义机器人 Webhook |
| 钉钉 | `notify.dingtalk` | 群自定义机器人 Webhook |
| Bark | `notify.bark` | [Bark App](https://github.com/Finb/Bark) |
| Email | `notify.email` | SMTP 服务器配置 |

## 项目结构

```
CardPulse/
├── bin/
│   └── cardpulse                 # 主入口
├── lib/
│   ├── config_reader.sh          # 配置读取
│   ├── at_modem.sh               # AT 命令通信
│   ├── sms_sender.sh             # 短信发送
│   ├── state_manager.sh          # 状态管理
│   └── notifier.sh               # 通知推送
├── config/
│   └── config.example.yaml       # 配置模板
├── scripts/
│   ├── install.sh                # 安装脚本
│   ├── cardpulse.service         # systemd 服务
│   └── cardpulse.timer           # systemd 定时器
├── DISCLAIMER.md                 # 法律声明
├── LICENSE                       # MIT 许可证
└── README.md                     # 项目说明
```

## 常见问题

### Q: 如何查看串口设备？

```bash
ls /dev/ttyUSB* /dev/ttyACM*
```

### Q: 权限不足怎么办？

```bash
# 将用户添加到 dialout 组
sudo usermod -aG dialout $USER

# 重新登录后生效
```

### Q: 如何调试 AT 命令？

```bash
# 使用 minicom 连接串口
sudo minicom -D /dev/ttyUSB0 -b 115200

# 关键 AT 命令
AT          # 握手
AT+CSQ      # 查看信号
AT+CREG?    # 查看网络注册
AT+CPIN?    # 查看 SIM 卡状态
```

### Q: GG 卡保号周期是多少天？

Google Voice 要求每 **180 天**至少有一次活动。CardPulse 默认设置为 179 天，留有余量。

### Q: 如何同时保多张卡？

目前 CardPulse 支持单卡。如需多卡，可以部署多个实例，使用不同的配置文件。

## 免责声明

本工具仅供个人学习与技术研究使用。使用本工具自动化发送短信可能违反 Google Voice 服务条款，导致号码被回收或帐户被封禁。**使用风险由使用者自行承担。**

详见 [DISCLAIMER.md](DISCLAIMER.md)。

## License

[MIT](LICENSE)
