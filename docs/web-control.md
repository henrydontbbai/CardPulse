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
