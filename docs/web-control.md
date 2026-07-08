# CardPulse Web Control

CardPulse Web follows the same control pattern as VoHive:

```text
Browser
  -> local HTTP API
  -> CardPulse Web backend
  -> cardpulse CLI / AT helper
  -> AT serial port
  -> 4G modem
```

The browser never talks to USB or serial devices directly. The local backend owns all device access.

## Start

From the CardPulse project root:

```bash
python3 scripts/cardpulse-web.py
```

Default URL:

```text
http://127.0.0.1:8765
```

For WSL testing from the Windows browser, bind to all interfaces:

```bash
python3 scripts/cardpulse-web.py --host 0.0.0.0 --port 8765
```

Keep this on a trusted local network only. The first version is a local operations panel, not an Internet-facing service.

## Features

- Device overview from `cardpulse --doctor` and `cardpulse --info`
- Raw command output for troubleshooting
- Read-only AT console with an allowlist
- SMS inbox status, listing, single-message read, and guarded single-message delete
- Guarded test SMS action

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

The current DJI/Baiwang QDC507 module reports SMS receive support through `AT+CSMS?`, `AT+CPMS?`, `AT+CMGF?`, and `AT+CNMI?`. If storage is full, for example `Storage: ME 23/23 FULL`, list the inbox first and delete only a known disposable message by explicit index.

Web delete requires the same `DELETE_SMS` confirmation token and only deletes one index per request.

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

For the current DJI/Baiwang `2ca3:4006` module, CardPulse Web should run inside the same Linux/WSL environment where `/dev/ttyUSB2` is available.

Recommended order:

```bash
cardpulse --doctor
cardpulse --info
python3 scripts/cardpulse-web.py --host 0.0.0.0
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

This performs the normal recovery path:

- keeps `Ubuntu-24.04` alive
- shares DJI/Baiwang USB bus `1-4` with `usbipd bind`, then attaches it to WSL
- runs `scripts/dji-qdc507-wsl-prepare.sh` inside WSL
- starts CardPulse Web at `http://127.0.0.1:8765`
- keeps SMS test disabled unless `-AllowSms` is explicitly passed

The helper checks `usbipd list` before binding. If the device is already `Shared` or `Attached`, it skips `bind`; otherwise, the first `usbipd bind` for a device may require an elevated Windows PowerShell prompt. After the device is shared once, later recovery runs can usually attach it without elevation.

If WSL `sudo` has no cached password, either run `sudo true` once inside Ubuntu first, or pass a local testing password with `-SudoPassword`. Do not store that password in the repository.

The script uses a stable WSL config path:

```text
~/.cardpulse-dji/config/config.yaml
```

If the stable config does not exist yet, it copies the existing `/tmp/cardpulse-dji-test/config/config.yaml` when available; otherwise it creates a safe local test config for `/dev/ttyUSB2`.
