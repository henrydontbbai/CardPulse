#!/usr/bin/env python3
"""Small local Web control surface for CardPulse."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 45

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CONTROL_RE = re.compile(r"[\r\n\x00-\x1f\x7f]")
READONLY_AT_COMMANDS = {
    "AT",
    "ATI",
    "AT+CGMI",
    "AT+CGMM",
    "AT+CGMR",
    "AT+CGSN",
    "AT+CSQ",
    "AT+CPIN?",
    "AT+CREG?",
    "AT+CGREG?",
    "AT+CEREG?",
    "AT+COPS?",
    "AT+CIMI",
    "AT+QCCID",
    "AT+CCID",
}

DEFAULT_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CardPulse Web</title>
</head>
<body>
  <main>
    <h1>CardPulse Web</h1>
    <p>Web assets are missing. API is available under /api.</p>
  </main>
</body>
</html>
"""


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        combined = "\n".join(part for part in (self.stdout, self.stderr) if part)
        return strip_ansi(combined).strip()


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value or "")


def parse_colon_lines(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in strip_ansi(output).splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace(" ", "_")
        value = value.strip()
        if key:
            parsed[key] = value
    return parsed


def parse_bool(value: str) -> bool | None:
    normalized = (value or "").strip().lower()
    if normalized in {"yes", "true", "1", "ready"}:
        return True
    if normalized in {"no", "false", "0"}:
        return False
    return None


def parse_int(value: str) -> int | None:
    try:
        return int((value or "").strip())
    except (TypeError, ValueError):
        return None


def parse_status_summary(parsed: dict[str, str]) -> dict[str, Any]:
    days_since = parse_int(first_parsed_value(parsed, ("days_since_last_send",)))
    interval_days = parse_int(first_parsed_value(parsed, ("interval_days",)))
    remaining_days = parse_int(first_parsed_value(parsed, ("remaining_days",)))
    send_due_raw = first_parsed_value(parsed, ("send_due",))
    send_due = parse_bool(send_due_raw)
    return {
        "last_send": first_parsed_value(parsed, ("last_send",)),
        "days_since_last_send": days_since,
        "interval_days": interval_days,
        "remaining_days": remaining_days,
        "send_due": bool(send_due) if send_due is not None else None,
        "next_send": first_parsed_value(parsed, ("next_send",)),
        "last_result": first_parsed_value(parsed, ("last_result",)),
    }


def parse_sms_status_summary(parsed: dict[str, str]) -> dict[str, Any]:
    storage_raw = first_parsed_value(parsed, ("storage",))
    storage_match = re.match(r"^(\S+)\s+(\d+)/(\d+)(?:\s+(FULL))?$", storage_raw)
    storage_name = ""
    storage_used = None
    storage_total = None
    storage_full = None
    if storage_match:
        storage_name = storage_match.group(1)
        storage_used = int(storage_match.group(2))
        storage_total = int(storage_match.group(3))
        storage_full = bool(storage_match.group(4)) or (
            storage_total > 0 and storage_used >= storage_total
        )
    return {
        "storage_name": storage_name,
        "storage_used": storage_used,
        "storage_total": storage_total,
        "storage_full": storage_full,
        "message_indication": first_parsed_value(parsed, ("new_message_indication",)),
        "message_format": first_parsed_value(parsed, ("format",)),
    }


def result_payload(result: CommandResult, *, parsed: Optional[dict[str, str]] = None) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "output": result.output,
        "parsed": parsed if parsed is not None else parse_colon_lines(result.output),
    }


def first_parsed_value(parsed: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = parsed.get(key)
        if value:
            return value
    return ""


def info_summary(parsed: dict[str, str]) -> dict[str, str]:
    return {
        "device": first_parsed_value(parsed, ("device", "\u8bbe\u5907")),
        "baudrate": first_parsed_value(parsed, ("baudrate", "\u6ce2\u7279\u7387")),
        "vendor": first_parsed_value(parsed, ("vendor", "\u5382\u5546")),
        "model": first_parsed_value(parsed, ("model", "\u578b\u53f7")),
        "imei": first_parsed_value(parsed, ("imei",)),
        "firmware": first_parsed_value(parsed, ("firmware", "version", "\u7248\u672c")),
        "sim": first_parsed_value(parsed, ("sim", "sim_card", "sim_\u5361")),
        "signal": first_parsed_value(parsed, ("signal", "rssi", "\u4fe1\u53f7\u5f3a\u5ea6")),
        "network": first_parsed_value(parsed, ("network", "network_registration", "\u7f51\u7edc\u72b6\u6001")),
        "operator": first_parsed_value(parsed, ("operator", "\u8fd0\u8425\u5546")),
    }


def is_readonly_at_command(cmd: str) -> bool:
    normalized = (cmd or "").strip().upper()
    if not normalized or CONTROL_RE.search(normalized):
        return False
    return normalized in READONLY_AT_COMMANDS


def is_sms_index(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9]+", value) is not None


class CardPulseRunner:
    def __init__(
        self,
        root_dir: Path = ROOT_DIR,
        timeout: int = DEFAULT_TIMEOUT,
        extra_env: Optional[dict[str, str]] = None,
    ):
        self.root_dir = Path(root_dir)
        self.timeout = timeout
        self.extra_env = extra_env or {}

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.extra_env)
        env.setdefault("CARDPULSE_LIB_DIR", str(self.root_dir / "lib"))
        return env

    def run_cardpulse(self, args: list[str]) -> CommandResult:
        configured_bin = os.environ.get("CARDPULSE_WEB_CARDPULSE_BIN", "").strip()
        if configured_bin:
            command = [configured_bin, *args]
        else:
            command = ["bash", str(self.root_dir / "bin" / "cardpulse"), *args]

        completed = subprocess.run(
            command,
            cwd=str(self.root_dir),
            env=self._env(),
            text=True,
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def run_at(self, cmd: str, timeout: int) -> CommandResult:
        script = r'''
set -euo pipefail
ROOT_DIR="${CARDPULSE_ROOT_DIR:?}"
source "$ROOT_DIR/lib/config_reader.sh"
source "$ROOT_DIR/lib/at_modem.sh"
source "$ROOT_DIR/lib/sms_sender.sh"
cleanup() {
    at_close 2>/dev/null || true
}
trap cleanup EXIT INT TERM
config_init
device="$(sms_detect_device || true)"
if [[ -z "$device" ]]; then
    echo "[ERROR] No AT serial port found." >&2
    exit 2
fi
baudrate="$(config_read ".serial.baudrate" "115200")"
if ! at_init "$device" "$baudrate"; then
    echo "[ERROR] Failed to open serial port." >&2
    exit 3
fi
at_send "$CARDPULSE_WEB_AT_CMD" "$CARDPULSE_WEB_AT_TIMEOUT"
'''
        env = self._env()
        env["CARDPULSE_ROOT_DIR"] = str(self.root_dir)
        env["CARDPULSE_WEB_AT_CMD"] = cmd
        env["CARDPULSE_WEB_AT_TIMEOUT"] = str(timeout)

        completed = subprocess.run(
            ["bash", "-c", script],
            cwd=str(self.root_dir),
            env=env,
            text=True,
            capture_output=True,
            timeout=max(timeout + 10, self.timeout),
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON body") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def make_handler(
    *,
    runner: CardPulseRunner,
    ui_path: Optional[Path],
    allow_sms: bool,
) -> type[BaseHTTPRequestHandler]:
    class CardPulseWebHandler(BaseHTTPRequestHandler):
        server_version = "CardPulseWeb/0.1"

        def log_message(self, fmt: str, *args: object) -> None:
            if os.environ.get("CARDPULSE_WEB_ACCESS_LOG") == "1":
                super().log_message(fmt, *args)

        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_html(self) -> None:
            if ui_path and ui_path.exists():
                body = ui_path.read_bytes()
            else:
                body = DEFAULT_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path in ("", "/"):
                    self.send_html()
                elif path == "/api/health":
                    self.send_json(200, {"status": "ok", "sms_enabled": allow_sms})
                elif path == "/api/doctor":
                    self.send_json(200, result_payload(runner.run_cardpulse(["--doctor"])))
                elif path == "/api/info":
                    payload = result_payload(runner.run_cardpulse(["--info"]))
                    payload.update(info_summary(payload["parsed"]))
                    self.send_json(200, payload)
                elif path == "/api/status":
                    payload = result_payload(runner.run_cardpulse(["--status"]))
                    payload.update(parse_status_summary(payload["parsed"]))
                    self.send_json(200, payload)
                elif path == "/api/sms/status":
                    payload = result_payload(runner.run_cardpulse(["--sms-status"]))
                    payload.update(parse_sms_status_summary(payload["parsed"]))
                    self.send_json(200, payload)
                elif path == "/api/sms/inbox":
                    self.send_json(200, result_payload(runner.run_cardpulse(["--inbox"])))
                elif path.startswith("/api/sms/messages/"):
                    index = path.rsplit("/", 1)[-1]
                    if not is_sms_index(index):
                        self.send_json(400, {"ok": False, "message": "SMS index must be a single non-negative integer"})
                        return
                    self.send_json(200, result_payload(runner.run_cardpulse(["--read-sms", index])))
                else:
                    self.send_json(404, {"ok": False, "message": "not found"})
            except subprocess.TimeoutExpired:
                self.send_json(504, {"ok": False, "message": "command timed out"})
            except Exception as exc:  # pragma: no cover - last-resort guard for local server
                self.send_json(500, {"ok": False, "message": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                data = read_json(self)
                if path == "/api/actions/test-sms":
                    self.handle_test_sms(data)
                elif path == "/api/at":
                    self.handle_at(data)
                elif path == "/api/sms/delete":
                    self.handle_delete_sms(data)
                else:
                    self.send_json(404, {"ok": False, "message": "not found"})
            except ValueError as exc:
                self.send_json(400, {"ok": False, "message": str(exc)})
            except subprocess.TimeoutExpired:
                self.send_json(504, {"ok": False, "message": "command timed out"})
            except Exception as exc:  # pragma: no cover - last-resort guard for local server
                self.send_json(500, {"ok": False, "message": str(exc)})

        def handle_test_sms(self, data: dict[str, Any]) -> None:
            if not allow_sms:
                self.send_json(403, {"ok": False, "message": "SMS test is disabled on this server"})
                return
            if data.get("confirm") != "SEND_SMS":
                self.send_json(400, {"ok": False, "message": "SMS test requires confirmation token SEND_SMS"})
                return
            self.send_json(200, result_payload(runner.run_cardpulse(["--test"])))

        def handle_at(self, data: dict[str, Any]) -> None:
            cmd = str(data.get("cmd", "")).strip()
            try:
                timeout = int(data.get("timeout", 5))
            except (TypeError, ValueError):
                timeout = 5
            timeout = max(1, min(timeout, 30))

            if not is_readonly_at_command(cmd):
                self.send_json(400, {"ok": False, "message": "AT command is not in the read-only allowlist"})
                return
            self.send_json(200, result_payload(runner.run_at(cmd, timeout)))

        def handle_delete_sms(self, data: dict[str, Any]) -> None:
            index = str(data.get("index", "")).strip()
            if not is_sms_index(index):
                self.send_json(400, {"ok": False, "message": "SMS index must be a single non-negative integer"})
                return
            if data.get("confirm") != "DELETE_SMS":
                self.send_json(400, {"ok": False, "message": "SMS delete requires confirmation token DELETE_SMS"})
                return
            self.send_json(200, result_payload(runner.run_cardpulse(["--delete-sms", index, "--confirm", "DELETE_SMS"])))

    return CardPulseWebHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local CardPulse Web control server.")
    parser.add_argument("--host", default=os.environ.get("CARDPULSE_WEB_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CARDPULSE_WEB_PORT", DEFAULT_PORT)))
    parser.add_argument("--root", default=str(ROOT_DIR), help="CardPulse repository or install root")
    parser.add_argument("--allow-sms", action="store_true", help="allow the Web UI to run cardpulse --test")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root_dir = Path(args.root).resolve()
    ui_path = root_dir / "web" / "index.html"
    allow_sms = args.allow_sms or os.environ.get("CARDPULSE_WEB_ALLOW_SMS") == "1"
    runner = CardPulseRunner(root_dir=root_dir)
    handler = make_handler(runner=runner, ui_path=ui_path, allow_sms=allow_sms)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}"
    print(f"CardPulse Web listening on {url}")
    if not allow_sms:
        print("SMS test action is disabled. Start with --allow-sms to enable the guarded test endpoint.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
