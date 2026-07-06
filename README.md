# CardPulse / 卡脉

> 让每一张 SIM 卡都有跳动的脉搏

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Shell](https://img.shields.io/badge/Shell-Bash-orange)](bin/cardpulse)

## 简介

CardPulse 是一个轻量级的 SIM 卡保活工具，通过 AT 命令直接控制 4G 模组发送定时短信，实现号码保活功能。

专为 **GG 卡 (Google Voice)** 等需要定期发送短信以防止号码回收的场景设计。

## 功能特性

- **完全独立**：无需任何外部服务，直接通过串口控制 4G 模组
- **PDU 短信编码**：默认使用 PDU 模式，支持英文、中文和 GSM 扩展字符
- **定时保号**：按设定天数自动发送短信，保持 SIM 卡活跃
- **智能调度**：仅在达到设定天数时发送，避免浪费
- **状态持久化**：记录上次发送时间，重启后继续计时
- **多渠道通知**：支持 Telegram、微信、企业微信、QQ、飞书、钉钉、Bark、Email
- **自动检测**：自动发现串口设备，支持手动配置
- **完善日志**：所有操作记录到日志文件，便于排查
- **CI 与容器化**：提供 GitHub Actions、Dockerfile 和本地测试入口

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

安装脚本默认优先配置 systemd timer；如果 systemd 不可用，则回退到 cron（每天凌晨 2 点执行）。

```bash
sudo systemctl start cardpulse.timer
sudo systemctl status cardpulse.timer
```

### Docker 使用

```bash
docker build -t cardpulse:local .
docker run --rm \
  --device /dev/ttyUSB0 \
  --group-add "$(getent group dialout | cut -d: -f3)" \
  -v "$HOME/.cardpulse:/home/cardpulse/.cardpulse" \
  cardpulse:local --status
```

容器默认使用非 root 用户 `cardpulse` 运行。实际发送短信时需要通过 `--device` 暴露串口设备，并挂载包含 `config.yaml` 的配置目录。若宿主机串口属于 `dialout` 组，建议用 `--group-add` 传入宿主机上的 `dialout` GID。

当前版本默认走单条 PDU 短信，不做长短信拆分；GSM 7-bit 最多 160 septets，UCS2 最多 140 octets。

### macOS 真机测试

macOS 可用于连接 4G 模组做 AT/PDU 真机验证，但不作为生产安装目标。建议先安装依赖：

```bash
brew install bash coreutils shellcheck python
python3 -m pip install pyyaml
```

常见串口为 `/dev/cu.usbserial*`、`/dev/cu.usbmodem*`、`/dev/cu.wchusbserial*` 或 `/dev/cu.SLAB_USBtoUART*`。配置时建议关闭自动检测并写入实际端口：

```yaml
serial:
  port: "/dev/cu.usbserial-XXXX"
  baudrate: 115200
  auto_detect: false
```

Linux 安装器、systemd、logrotate 和生产调度仍以 Linux 环境验证为准。

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
  --notify-channel CH  测试指定通知渠道
  --doctor             运行硬件诊断；不发送短信、不写入状态
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
│   ├── notifier.sh               # 通知推送
│   └── pdu_encoder.py            # PDU 编码器
├── config/
│   └── config.example.yaml       # 配置模板
├── scripts/
│   ├── install.sh                # 安装脚本
│   ├── cardpulse.service         # systemd 服务
│   └── cardpulse.timer           # systemd 定时器
├── tests/                        # 本地测试
├── DISCLAIMER.md                 # 法律声明
├── LICENSE                       # MIT 许可证
└── README.md                     # 项目说明
```

## 常见问题

### Q: 如何运行本地测试？

```bash
sudo apt-get install -y python3-yaml shellcheck
bash tests/run.sh
```

`tests/run.sh` 会检查 Python 语法、版本一致性、配置 schema、PDU 编码和 Shell 语法。

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

## 硬件诊断与 macOS 测试

使用 `--info` 或发送短信前，先确认 4G 模块暴露了 AT 串口。系统能看到 USB 设备不等于 CardPulse 能使用。

```bash
cardpulse --doctor
```

`--doctor` 不会发送短信，也不会写入 CardPulse 状态。如果存在 AT 串口，它会发送 `AT` 等查询命令确认模块是否返回 `OK`。

在 macOS 上，先找 `/dev/cu.usbserial*` 或 `/dev/cu.usbmodem*`。如果 DJI/Baiwang `2CA3:4006` 只出现在 USB 列表里，但没有 AT 串口，CardPulse 还不能直接使用它。

在 Linux 上，`--doctor` 还会报告 `/dev/cdc-wdm*` 这类 MBIM/QMI 控制口。这能解释为什么 VoHive 这类工具可能可用，但不代表 CardPulse 的 AT 串口路径已经就绪。详见 `docs/hardware-diagnostics.md`。

Apple Silicon Mac 可以用 Ubuntu ARM64 虚拟机加 USB 直通验证 DJI 模块；如果 VM 内出现 MBIM/QMI 控制口，再构建 VoHive `linux_arm64` 做只读发现。不要运行现有 `linux_amd64` 二进制，也不要在未授权时发送真实短信。

## License

[MIT](LICENSE)
