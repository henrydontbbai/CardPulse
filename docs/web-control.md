# CardPulse Web Control

Local development and diagnostic CardPulse Web follows this control pattern:

```text
Browser
  -> local HTTP API
  -> CardPulse Web backend
  -> cardpulse CLI / AT helper
  -> AT serial port
  -> 4G modem
```

The browser never talks to USB or serial devices directly. The local backend owns all device access.

## 飞牛 FPK Web 入口

飞牛 FPK 中，浏览器只能经飞牛管理员统一网关 `/app/cardpulse` 进入。网关转发到
`${TRIM_APPDEST}/app.sock`，其容器对应路径为 `/run/cardpulse/app.sock`；应用不开放
LAN TCP 监听，也不使用 Caddy。fnOS 管理员身份取代 FPK 模式下的第二个 CardPulse
密码门禁。

Web 以 `--base-path /app/cardpulse` 运行，所有 API、静态资源、Cookie、CSRF 和重定向
都受该前缀约束，不能从其他路径取得会话。Web 启动时会执行一次 `--sms-status` 作为
收件箱容量基线审计；它不发送也不删除短信。

首次 FPK 安装默认是 no-device。管理员只有在宿主已完成 QDC507 驱动/稳定别名预置、
存在已验证的 `/dev/cardpulse-at`，并在安装向导或配置中显式选择设备模式后，才会启用
这一个固定设备映射。缺失时应用保持无设备降级，不会扫描其他串口。

调度默认关闭，且容器重启和应用升级后仍保持关闭。设置页仅在收件人、内容和周期有效，
并且 `/var/lib/cardpulse/lifecycle/qdc507-readonly-acceptance.json` 所代表的 QDC507
只读验收通过时，才允许飞牛管理员显式开启。该记录由 fnOS lifecycle 身份写入，runtime
只能读取；它还会分别与运行容器固定的 `CARDPULSE_RUNTIME_VERSION`、
`CARDPULSE_RUNTIME_IMAGE`（完整 digest）和 `CARDPULSE_FPK_VERSION` 比对。调度器在
每次轮询前也会复核该记录；镜像或 FPK 版本变化会使旧记录失效，记录缺失或无效时不会发送。真实发送仍需要额外明确授权。

56 号 NAS 尚未完成 FPK 安装、网关 socket、设备透传或 QDC507 设备 POC。QDC507 宿主
预置当前已验证：`2ca3:4006` 的 AT TTY 是 `/dev/ttyUSB3`，稳定别名
`/dev/cardpulse-at` 已存在并指向该设备。持久驱动绑定是管理员独立维护的宿主 helper 与
USB udev 规则，FPK 不会创建、调用或管理它们。post-helper 物理拔插、AT/SIM/网络注册
已经验证；重启、非 root 容器访问和 FPK 实机验收仍不是已完成事实。完整部署边界与只读
POC 路径见
[飞牛 NAS 部署](nas-deployment.md)。

## Local development or diagnostic start

From the CardPulse project root:

```bash
python3 scripts/cardpulse-web.py
```

Default URL:

```text
http://127.0.0.1:8765
```

This local listener is not a NAS product entry point. WSL is diagnostic-only in this phase.
Its Web listener stays on loopback.

Before starting a local development or diagnostic Web service, create its password hash interactively:

```bash
python3 scripts/cardpulse-web-password.py
```

The password is stored only as an scrypt hash in `web-auth.json`. In local
development or diagnostic mode, the browser must log in before it can view
modem data, SMS content, local history, or execute an operation. The fnOS FPK
uses the `/app/cardpulse` administrator gateway instead and does not require a
second CardPulse password.

## Features

- Device overview from `cardpulse --doctor` and `cardpulse --info`
- Raw command output for troubleshooting
- Read-only AT console with an allowlist
- SMS inbox status, listing, single-message read, guarded single-message delete, and guarded batch delete
- Guarded test SMS action

When the Overview page or Message Center is visible, the page refreshes the relevant read-only data every 60 seconds. It skips hidden tabs and never overlaps an in-progress refresh. Messages are shown newest first; selecting a message remains highlighted across refreshes.

The read-only AT console accepts only safe query commands such as:

```text
AT
ATI
AT+CSQ
AT+CPIN?
AT+CREG?
AT+CGREG?
AT+CEREG?
AT+COPS?
AT+CIMI
AT+QCCID
AT+CCID
```

Commands that can change modem state or send SMS, such as `AT+CFUN=0` and `AT+CMGS=...`, are blocked by default.

## SMS Inbox

The inbox view calls the safe CLI receive commands:

```bash
cardpulse --sms-status
cardpulse --inbox
cardpulse --read-sms INDEX
cardpulse --delete-sms INDEX --confirm DELETE_SMS
```

Inbox reads do not delete messages and do not persist message bodies to CardPulse state. Reading an unread SMS may still cause the modem to mark that SMS as read.

Concatenated messages are shown as one logical message with all physical `Indexes`, complete/incomplete status, and a joined body. Opening a complete concatenated message only records the local “viewed” acknowledgement; it does not issue an invalid single-index modem read.

The Web UI uses a native confirmation dialog for every real SMS send and delete action. Single delete still requires `DELETE_SMS` and deletes exactly one physical index. Batch delete requires `DELETE_SMS_BATCH`, accepts at most five physical slots, re-reads the inbox before deleting, rejects partial concatenated groups, and deletes indexes in descending order. An incomplete concatenated message remains excluded from normal batch deletion: its detail pane offers a separate force-delete action only after `FORCE_DELETE_INCOMPLETE_SMS` is entered. The server re-reads the inbox and permits that endpoint only when the requested slots exactly match one current incomplete multipart group. This irreversible cleanup deletes only the residual physical slots in descending order. Local message history is retained.

There is no automatic full-store cleanup. A full store raises a capacity alert; it never issues a module delete command by itself.

The Web service writes `state/sms-operations.jsonl` for traceability. It records the SMS storage baseline and each real manual single delete, batch delete, or force-delete of an incomplete multipart group using only timestamp, operation result, counts, verification state, and storage capacity. It never records message bodies, previews, phone numbers, or SMS indexes. Rejected requests that do not execute a modem deletion are not appended.

Local message history is retained for 90 days from trusted local `first_seen_at`, not from the modem PDU timestamp. Existing JSONL records without that timestamp are discarded during migration. The Message Center can clear local history after entering `CLEAR_LOCAL_HISTORY`; this action does not call the CLI, AT helper, or module delete command.

Recommended receive test loop:

```bash
cardpulse --sms-status
cardpulse --inbox
# send one external SMS to the SIM from another phone
cardpulse --inbox
cardpulse --read-sms INDEX
```

If the store is full again, delete exactly one known disposable index first:

```bash
cardpulse --delete-sms INDEX --confirm DELETE_SMS
```

## SMS Safety Gate

The Web UI cannot send a real SMS unless the server is started with an explicit SMS gate:

```bash
python3 scripts/cardpulse-web.py --allow-sms
```

Even after that, the UI requires the confirmation token:

```text
SEND_SMS
```

The SMS action runs:

```bash
cardpulse --test
```

That command sends a real SMS using the current `config.yaml`.

## DJI / Baiwang WSL Route

For the current DJI/Baiwang `2ca3:4006` module, the following is a local
Windows/WSL diagnostic route only. It is not the fnOS product deployment path.
Run it only on a system where the operator has the required device-write
authorization.

Recommended order:

```bash
cardpulse --doctor
cardpulse --info
cardpulse --sms-status
python3 scripts/cardpulse-web.py --host 127.0.0.1
```

If the module is detached and reattached, rebind the Linux `option` serial driver first, then restart or refresh the Web page.

Helper command inside WSL:

```bash
sudo bash scripts/dji-qdc507-wsl-prepare.sh
```

## One-command Windows recovery

From a Windows PowerShell prompt in the CardPulse project root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-dji-wsl-web.ps1
```

The verified hardware route uses `8766` by default:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-dji-wsl-web.ps1 -Port 8766
```

This performs the normal recovery path:

- keeps `Ubuntu-24.04` alive
- finds the DJI/Baiwang `2CA3:4006` USB BusId from `usbipd list`, unless `-BusId` is provided
- shares that USB device with `usbipd bind`, then attaches it to WSL
- runs `scripts/dji-qdc507-wsl-prepare.sh` inside WSL
- starts CardPulse Web at `http://127.0.0.1:8766` by default
- verifies AT, SIM READY, usable RSSI, network registration, and Web status
- keeps SMS test disabled unless `-AllowSms` is explicitly passed

The recovery script does not terminate an existing Web process. If `-Port 8766` is already in use, it reports the owning process and exits; choose another port or stop that specific service yourself.

The helper checks `usbipd list` before binding. If the device is already `Shared` or `Attached`, it skips `bind`; otherwise, the first `usbipd bind` for a device may require an elevated Windows PowerShell prompt. After the device is shared once, later recovery runs can usually attach it without elevation.

When the Linux serial driver needs rebinding, the helper prompts in the current PowerShell window for the Ubuntu `sudo` password. The password is handled only by `sudo`; the script does not accept it as an argument or store it in the repository, environment, or recovery state.

The script uses a stable WSL config path:

```text
~/.cardpulse-dji/config/config.yaml
```

If the stable config does not exist yet, the script creates a safe local test config for `/dev/ttyUSB2` under `~/.cardpulse-dji/`. It no longer depends on temporary test paths.

The same recovery flow now writes the latest recovery result to:

```text
~/.cardpulse-dji/state/recovery.json
```

The recovery file includes the latest `state`, `step`, `phase_status`, `summary`, `operator_hint`, `port`, `web_url`, `busid`, and `distro`. The Web overview uses these fields to show whether recovery is currently in USB attach, driver binding, AT doctor, or Web health verification.

This file keeps a single latest-state snapshot for the Web overview page:

- `state`: `ok` or `error`
- `summary`: short recovery result summary
- `checked_at`: latest recovery check time
- `port`: detected AT serial port such as `/dev/ttyUSB2`
- `web_url`: latest verified Web URL

The Web overview reads that file and shows the latest recovery status on the home page. This is a current-state file, not a recovery history log.
