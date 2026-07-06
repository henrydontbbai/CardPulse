# 硬件诊断

CardPulse 通过 AT 串口和 4G 模块通信。系统里出现 USB 设备不等于 CardPulse 可用。

部分模块也可以通过 Linux MBIM/QMI 控制口工作，例如 `/dev/cdc-wdm*` 或 `/dev/wwan*qmi*`。这是另一条后端路线，不是 CardPulse 当前的 AT 串口实现。

## CardPulse 可用标准

只有同时满足这些条件，模块才算 CardPulse 可用：

1. 系统暴露串口设备，例如 `/dev/ttyUSB*`、`/dev/ttyACM*`、`/dev/cu.usbserial*` 或 `/dev/cu.usbmodem*`。
2. 串口能按配置波特率打开。
3. 发送 `AT` 返回 `OK`。
4. `AT+CPIN?` 返回 `READY`。
5. `AT+CREG?` 显示已注册网络，通常是 `1` 或 `5`。

先运行安全诊断：

```bash
cardpulse --doctor
```

`--doctor` 不发送短信，也不写入 CardPulse 状态。如果存在 AT 串口，它会发送 `AT`、`AT+CPIN?`、`AT+CSQ`、`AT+CREG?` 这类查询命令。

## macOS 测试顺序

macOS 原生只做 AT 串口验证：

```bash
ls /dev/cu.usb* /dev/cu.wchusbserial* /dev/cu.SLAB_USBtoUART* 2>/dev/null
cardpulse --doctor
cardpulse --info
```

只有 `--doctor` 报告 `AT: OK` 后，才运行 `cardpulse --info`。

不要直接运行 `cardpulse --test`；它会发送真实短信，必须单独确认后再执行。

## Linux MBIM/QMI 检查

在 Linux 上，`cardpulse --doctor` 还会列出 MBIM/QMI 候选设备：

```bash
ls /dev/cdc-wdm* /dev/wwan* 2>/dev/null
cardpulse --doctor
```

如果 `--doctor` 发现 Linux MBIM/QMI 控制口但没有 AT 串口，硬件可能仍能通过 VoHive 这类 MBIM/QMI 工具使用。CardPulse 仍会报告它未就绪，直到出现 AT 串口，或 CardPulse 增加 MBIM/QMI 后端。

## Apple Silicon Mac + Ubuntu VM 路线

Apple Silicon Mac 可以作为宿主机，但真实验证要在 Ubuntu ARM64 虚拟机内完成，并把 DJI USB 设备直通给虚拟机。

只读验收顺序：

```bash
uname -m
lsusb
ls /dev/cdc-wdm* /dev/wwan* /dev/ttyUSB* 2>/dev/null
lsmod | grep -E 'qmi_wwan|cdc_mbim|wwan'
```

如果 VM 内出现 `/dev/cdc-wdm*` 或明确的 `qmi_wwan`、`cdc_mbim`、`wwan_qmi` 绑定，再构建 VoHive `linux_arm64` 版本做只读发现。不要运行现有 `linux_amd64` 二进制，也不要在未授权时发短信。

## DJI / Baiwang 2CA3:4006 状态

当前观察到的 DJI/Baiwang 硬件枚举为：

- USB product: `Baiwang`
- USB VID: `2CA3`
- USB PID: `4006`

这只能证明 Mac 看到了 USB 设备，不能证明 CardPulse 可用。当前测试中，它没有暴露 `/dev/cu.usbserial*` 或 `/dev/cu.usbmodem*` 这类 AT 串口。

VoHive 这类工具可能依赖 Linux MBIM/QMI 路线，而不是 macOS AT 串口路线。有效 Linux 信号是绑定 `cdc_mbim`、`qmi_wwan` 或 `wwan_qmi` 驱动，并出现 `/dev/cdc-wdm0` 这类控制口。

在出现 AT 串口并对 `AT` 返回 `OK` 之前，DJI/Baiwang `2CA3:4006` 不属于 CardPulse 已验证支持硬件。

## 更快跑通闭环的硬件方向

如果目标是最快跑通 CardPulse 端到端测试，优先使用明确暴露 AT 串口的模块：

- Quectel EC20 / EC25
- Huawei ME909
- SIMCom SIM7600
- 文档明确支持 AT 指令的 USB 短信猫

Linux 仍是推荐生产目标。macOS 适合做人工硬件验证，但前提是模块暴露了兼容 AT 串口。
