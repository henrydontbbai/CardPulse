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

> **飞牛 NAS 安装入口**：CardPulse 的目标生产形态是手动上传 `.fpk`，再从飞牛
> 统一网关 `/app/cardpulse` 打开应用。FPK 不开放 LAN TCP 端口、不使用 Caddy，且
> 首装默认 no-device 与关闭调度。宿主已验证 `/dev/cardpulse-at` 后，飞牛管理员才能
> 启用这一个固定设备映射；QDC507 只读验收完成后，才能启用调度。当前尚未在 56 号 NAS
> 完成真实安装或 QDC507 验收；不要把本节的原生安装器、Caddy 或 `systemd`/cron
> 命令用于 FPK。详见 [飞牛 NAS 部署](docs/nas-deployment.md)。

### 1. 安装 CardPulse（获授权的非飞牛本地 Linux 开发或诊断）

以下命令仅适用于你拥有写入许可的本地 Linux 开发或诊断主机，不适用于飞牛 FPK，
也不得在当前 56 号 NAS 的只读核验窗口执行。

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
# 先做安全硬件诊断（不发短信、不写状态）
cardpulse --doctor

# 查看模组信息
cardpulse --info

# 真实短信测试：仅在确认授权后执行
# cardpulse --test

# 查看状态
cardpulse --status
```

### 4. 设置定时任务（非飞牛 NAS）

这一节仅适用于拥有写入许可的非飞牛 Linux 本地开发或诊断环境。飞牛 FPK 不使用
`deploy/nas/cardpulse.timer`、Caddy 或 cron；其唯一调度器在容器内运行，并在
首次安装、容器重启和升级后保持关闭，直到完成 QDC507 只读验收且由飞牛管理员
显式开启。

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

Linux 安装器、systemd 和 logrotate 只适用于非飞牛 NAS 的本地 Linux 环境。

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

## CardPulse Web 控制台

CardPulse 现在提供一个轻量本地 Web 控制台：浏览器访问本地 HTTP API，本地后端再调用 CardPulse CLI / AT 串口逻辑；网页本身不直接访问 USB。

```bash
python3 scripts/cardpulse-web.py
```

默认地址为 `http://127.0.0.1:8765`。飞牛 FPK 的生产入口是统一网关
`/app/cardpulse`，经 Unix socket 接入容器；不要把开发或 WSL Web 直接绑定到
局域网接口。

当前 Windows + WSL2 一键恢复脚本只用于硬件诊断和恢复验证：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-dji-wsl-web.ps1
```

Web 默认提供状态、诊断、消息中心和只读 AT 查询；真实短信发送默认关闭，只有显式启用后才开放受控发送。

WSL 在当前阶段仅用于硬件诊断和恢复验证。`docs/web-control.md` 说明该诊断路径：

- 一键恢复脚本
- `recovery.json` 恢复状态文件
- Web 首页与消息中心
- 日常检查顺序与短信删除安全约束

产品定位与下一阶段方向见 [docs/product-direction.md](docs/product-direction.md)。

## 飞牛 NAS 产品部署

飞牛 NAS 是 CardPulse 的目标生产宿主；Windows/WSL 仅用于本地开发与诊断。产品入口
是飞牛应用中心的 CardPulse 与统一网关 `/app/cardpulse`，应用经 Unix socket 接入
网关，不开放 LAN TCP 端口，也不使用 Caddy。首次安装默认不映射设备；只有管理员
选择设备模式且宿主已有验证的 `/dev/cardpulse-at` 时，才会映射这一个固定设备。

QDC507 驱动和 `udev` 稳定别名由宿主在单独授权的维护窗口预置，FPK 不修改宿主驱动或
规则。安装或升级发现旧原生 CardPulse 路径、`systemd` 单元或 cron 条目时，FPK 会
拒绝继续且不修改宿主服务。QDC507 只读验收、升级/卸载保留行为以及设备拔插和重启
稳定性尚未在 56 上完整实机验证；其中宿主的物理拔插、AT、SIM 与网络注册已通过，
但打包后的设备 POC、升级/卸载和重启仍待验收。详情见
[飞牛 NAS 部署](docs/nas-deployment.md)。

56 的持久驱动绑定使用管理员单独维护的 root-owned helper 和 USB `add` udev 规则；它们
只加载 `option` 并注册 QDC507 的动态 USB ID，不运行 AT 或短信操作，也不是 FPK 资产。
构建、安装、升级、卸载和 runtime 均不得创建、修改或调用这些宿主文件。

## 短信接收 / 收件箱

CardPulse 当前支持安全收件箱命令：

```bash
cardpulse --sms-status
cardpulse --inbox
cardpulse --read-sms 1
cardpulse --delete-sms 1 --confirm DELETE_SMS
```

默认行为仍是只读：读取不会自动删除短信，也不会把短信正文写入状态文件；满仓只会告警，不会自动删除。单条删除必须显式指定索引并输入 `DELETE_SMS`。Web 消息中心也支持最多 5 个物理槽位的人工批量删除，需输入 `DELETE_SMS_BATCH`，且不会接受不完整的拼接短信组。残缺拼接短信只能在详情页通过独立的“强制删除”操作处理：服务端会重新读取收件箱、确认所选槽位仍恰好属于同一残缺拼接组，并要求输入 `FORCE_DELETE_INCOMPLETE_SMS`；此操作不可逆。每次人工删除成功后都会复读收件箱与容量，只有确认对应槽位消失才标记为已验证。操作审计只保存数量、结果和验证状态，不保存正文、预览、号码或短信索引。

关于 Web 消息中心、首页容量告警、恢复状态以及 Windows + WSL 日常运维顺序，统一见 [docs/web-control.md](docs/web-control.md)。

## License

[MIT](LICENSE)
