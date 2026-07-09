#!/usr/bin/env python3
"""Small local Web control surface for CardPulse."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 45

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CONTROL_RE = re.compile(r"[\r\n\x00-\x1f\x7f]")
MESSAGE_BLOCK_KEYS = {
    "index",
    "status",
    "from",
    "to",
    "time",
    "preview",
    "message",
}
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
PHONE_RE = re.compile(r"^\+?[0-9][0-9 -]{4,30}[0-9]$")
MAX_WEB_SMS_LENGTH = 612

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


def parse_message_blocks(output: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    last_key = ""
    for raw_line in strip_ansi(output).splitlines():
        line = raw_line.rstrip()
        if not line or line.startswith("==="):
            if current:
                blocks.append(current)
                current = {}
                last_key = ""
            continue
        if last_key == "message" and current:
            current[last_key] = current[last_key] + "\n" + line.strip()
            continue
        if ":" not in line:
            if current and last_key == "preview":
                current[last_key] = current[last_key] + "\n" + line.strip()
            continue
        key, value = line.split(":", 1)
        normalized = key.strip().lower().replace(" ", "_")
        if current and last_key == "preview" and normalized not in MESSAGE_BLOCK_KEYS:
            current[last_key] = current[last_key] + "\n" + line.strip()
            continue
        if normalized:
            current[normalized] = value.strip()
            last_key = normalized
    if current:
        blocks.append(current)
    return blocks


def message_id(direction: str, phone: str, timestamp: str, body: str, index: str = "") -> str:
    source = "\x1f".join([direction, phone, timestamp, body, index])
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def module_message_id(direction: str, phone: str, timestamp: str, index: str) -> str:
    return message_id(direction, phone, timestamp, "", index)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def unique_message_id(direction: str, phone: str, timestamp: str, body: str, index: str = "") -> str:
    return message_id(direction, phone, timestamp, body, index or uuid.uuid4().hex)


def validate_web_sms(phone: str, message_text: str) -> str:
    if not phone:
        return "phone is required"
    if not PHONE_RE.match(phone):
        return "phone must be an international-style number"
    if not message_text:
        return "message is required"
    if len(message_text) > MAX_WEB_SMS_LENGTH:
        return f"message must be {MAX_WEB_SMS_LENGTH} characters or fewer"
    if CONTROL_RE.search(message_text):
        return "message must not contain control characters"
    return ""


def normalize_inbox_entry(entry: dict[str, str]) -> dict[str, Any]:
    index = entry.get("index", "")
    sender = entry.get("from", "")
    timestamp = entry.get("time", "")
    preview = entry.get("preview", "")
    status = entry.get("status", "")
    return {
        "id": module_message_id("inbound", sender, timestamp, index),
        "index": index,
        "direction": "inbound",
        "from": sender,
        "to": "",
        "phone": sender,
        "time": timestamp,
        "status": status,
        "preview": preview,
        "body": preview,
        "storage": "module",
        "source": "receive",
    }


def normalize_sms_message(entry: dict[str, str], *, index: str) -> dict[str, Any]:
    sender = entry.get("from", "")
    timestamp = entry.get("time", "")
    body = entry.get("message", entry.get("preview", ""))
    status = entry.get("status", "")
    return {
        "id": module_message_id("inbound", sender, timestamp, index),
        "index": index,
        "direction": "inbound",
        "from": sender,
        "to": "",
        "phone": sender,
        "time": timestamp,
        "status": status,
        "preview": body[:80] + ("..." if len(body) > 80 else ""),
        "body": body,
        "storage": "module",
        "source": "receive",
    }


class MessageHistory:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._messages: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                data = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("id"):
                self._messages[str(data["id"])] = data

    def _persist(self, message: dict[str, Any]) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message, ensure_ascii=False, sort_keys=True) + "\n")

    def _rewrite(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for message in self._messages.values():
                handle.write(json.dumps(message, ensure_ascii=False, sort_keys=True) + "\n")
        tmp_path.replace(self.path)

    def add(self, message: dict[str, Any]) -> dict[str, Any]:
        stored = dict(message)
        stored.setdefault("id", message_id(
            str(stored.get("direction", "")),
            str(stored.get("phone", "")),
            str(stored.get("time", "")),
            str(stored.get("body", "")),
            str(stored.get("index", "")),
        ))
        with self._lock:
            existing = self._messages.get(stored["id"])
            if existing:
                merged = dict(existing)
                merged.update({key: value for key, value in stored.items() if value not in ("", None)})
                if stored.get("body") and len(str(stored.get("body", ""))) >= len(str(existing.get("body", ""))):
                    merged["body"] = stored["body"]
                    merged["preview"] = stored.get("preview") or existing.get("preview", "")
                self._messages[stored["id"]] = merged
                if merged != existing:
                    self._rewrite()
                return merged
            self._messages[stored["id"]] = stored
            self._persist(stored)
            return stored

    def list(self, direction: str = "") -> list[dict[str, Any]]:
        with self._lock:
            messages = list(self._messages.values())
        if direction in {"inbound", "outbound"}:
            messages = [item for item in messages if item.get("direction") == direction]
        return sorted(messages, key=lambda item: (str(item.get("time", "")), str(item.get("id", ""))))

    def get(self, message_id_value: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._messages.get(message_id_value)
            return dict(item) if item else None


def severity_rank(value: str) -> int:
    return {"ok": 0, "warn": 1, "danger": 2}.get(value, 1)


def strongest_severity(values: list[str]) -> str:
    return max(values, key=severity_rank) if values else "warn"


def build_overview_payload(
    *,
    info: dict[str, Any],
    status: dict[str, Any],
    sms_status: dict[str, Any],
    sms_enabled: bool,
) -> dict[str, Any]:
    info_ok = bool(info.get("ok"))
    status_ok = bool(status.get("ok"))
    sms_status_ok = bool(sms_status.get("ok"))
    signal_value = parse_int(str(info.get("signal", "")))
    network_value = str(info.get("network", "") or "").strip()
    sim_status = str(info.get("sim", "") or "").strip()

    connection = {
        "port": info.get("device", ""),
        "state": "ok" if info.get("device") and info_ok else "danger",
    }
    sim = {
        "status": sim_status,
        "ready": sim_status.upper() == "READY",
    }
    signal = {
        "rssi": signal_value,
        "state": "ok" if signal_value is not None and signal_value != 99 else "danger",
    }
    registration = {
        "code": network_value,
        "registered": network_value in {"1", "5"},
    }

    storage_used = sms_status.get("storage_used")
    storage_total = sms_status.get("storage_total")
    remaining = None
    storage_severity = "warn"
    if not sms_status_ok:
        storage_severity = "danger"
    elif isinstance(storage_used, int) and isinstance(storage_total, int):
        remaining = max(0, storage_total - storage_used)
        if sms_status.get("storage_full") or remaining == 0:
            storage_severity = "danger"
        elif remaining <= 3:
            storage_severity = "warn"
        else:
            storage_severity = "ok"

    send_due = status.get("send_due")
    remaining_days = status.get("remaining_days")
    last_result = str(status.get("last_result", "") or "")
    if not status_ok:
        keepalive_state = "warn"
        keepalive_summary = "保号状态读取失败，请查看原始输出。"
    elif send_due is True:
        keepalive_state = "warn"
        keepalive_summary = "今天应发送保号短信"
    elif last_result and last_result != "success":
        keepalive_state = "warn"
        keepalive_summary = f"上次保号结果异常：{last_result}"
    elif isinstance(remaining_days, int):
        keepalive_state = "ok"
        keepalive_summary = f"保号正常，距离下次发送还有 {remaining_days} 天"
    else:
        keepalive_state = "warn"
        keepalive_summary = "保号状态未知，请先运行一次状态检查"

    component_states = [
        connection["state"],
        "ok" if sim["ready"] else "danger",
        signal["state"],
        "ok" if registration["registered"] else "danger",
        storage_severity,
        keepalive_state,
    ]
    overall_status = strongest_severity(component_states)

    command_failures = []
    if not info_ok:
        command_failures.append("模组信息")
    if not status_ok:
        command_failures.append("保号状态")
    if not sms_status_ok:
        command_failures.append("短信存储")

    recommended_action = "状态正常，继续保持本地 Web 只读运维即可。"
    if command_failures:
        recommended_action = " / ".join(command_failures) + "读取失败，请查看原始输出并重新运行 WSL 恢复脚本。"
    elif connection["state"] != "ok":
        recommended_action = "设备端口不可用，请重新运行 WSL 恢复脚本。"
    elif not sim["ready"]:
        recommended_action = "SIM 未 READY，请检查 SIM 卡和模块状态。"
    elif signal["state"] != "ok" or not registration["registered"]:
        recommended_action = "网络未稳定注册，请检查信号、天线或运营商状态。"
    elif storage_severity == "danger":
        recommended_action = "短信存储已满，请读取收件箱并删除 1 条旧短信后再接收新短信。"
    elif storage_severity == "warn":
        recommended_action = "短信存储接近满仓，建议清理明确无用的旧短信。"
    elif keepalive_state != "ok":
        recommended_action = keepalive_summary

    raw = {
        "info": info.get("output", ""),
        "status": status.get("output", ""),
        "sms_status": sms_status.get("output", ""),
    }
    raw_blocks = []
    if raw["status"]:
        raw_blocks.append(f"=== status ===\n{raw['status']}")
    if raw["info"]:
        raw_blocks.append(f"=== info ===\n{raw['info']}")
    if raw["sms_status"]:
        raw_blocks.append(f"=== sms_status ===\n{raw['sms_status']}")

    return {
        "ok": info_ok and status_ok and sms_status_ok,
        "overall_status": overall_status,
        "recommended_action": recommended_action,
        "sms_enabled": sms_enabled,
        "connection": connection,
        "sim": sim,
        "signal": signal,
        "registration": registration,
        "operator": info.get("operator", ""),
        "imei": info.get("imei", ""),
        "sms_storage": {
            "name": sms_status.get("storage_name", ""),
            "used": storage_used,
            "total": storage_total,
            "remaining": remaining,
            "full": sms_status.get("storage_full"),
            "severity": storage_severity,
            "format": sms_status.get("message_format", ""),
        },
        "keepalive": {
            "state": keepalive_state,
            "summary": keepalive_summary,
            "last_send": status.get("last_send", ""),
            "next_send": status.get("next_send", ""),
            "last_result": last_result,
            "send_due": send_due,
            "remaining_days": remaining_days,
        },
        "raw": raw,
        "output": "\n\n".join(raw_blocks),
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

    def send_message(self, phone: str, message: str) -> CommandResult:
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
if ! sms_validate_preconditions; then
    exit 4
fi
sms_send_with_retry "$CARDPULSE_WEB_SMS_PHONE" "$CARDPULSE_WEB_SMS_MESSAGE"
'''
        env = self._env()
        env["CARDPULSE_ROOT_DIR"] = str(self.root_dir)
        env["CARDPULSE_WEB_SMS_PHONE"] = phone
        env["CARDPULSE_WEB_SMS_MESSAGE"] = message
        completed = subprocess.run(
            ["bash", "-c", script],
            cwd=str(self.root_dir),
            env=env,
            text=True,
            capture_output=True,
            timeout=self.timeout,
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
    history_path: Optional[Path] = None,
) -> type[BaseHTTPRequestHandler]:
    history = MessageHistory(history_path)

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
            parsed_url = urlparse(self.path)
            path = parsed_url.path
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
                elif path == "/api/overview":
                    info_payload = result_payload(runner.run_cardpulse(["--info"]))
                    info_payload.update(info_summary(info_payload["parsed"]))
                    status_payload = result_payload(runner.run_cardpulse(["--status"]))
                    status_payload.update(parse_status_summary(status_payload["parsed"]))
                    sms_payload = result_payload(runner.run_cardpulse(["--sms-status"]))
                    sms_payload.update(parse_sms_status_summary(sms_payload["parsed"]))
                    self.send_json(
                        200,
                        build_overview_payload(
                            info=info_payload,
                            status=status_payload,
                            sms_status=sms_payload,
                            sms_enabled=allow_sms,
                        ),
                    )
                elif path == "/api/sms/inbox":
                    self.send_json(200, result_payload(runner.run_cardpulse(["--inbox"])))
                elif path.startswith("/api/sms/messages/"):
                    index = path.rsplit("/", 1)[-1]
                    if not is_sms_index(index):
                        self.send_json(400, {"ok": False, "message": "SMS index must be a single non-negative integer"})
                        return
                    self.send_json(200, result_payload(runner.run_cardpulse(["--read-sms", index])))
                elif path == "/api/messages/current":
                    self.handle_current_messages()
                elif path.startswith("/api/messages/current/"):
                    index = path.rsplit("/", 1)[-1]
                    self.handle_current_message(index)
                elif path == "/api/messages/history":
                    params = parse_qs(parsed_url.query)
                    direction = params.get("direction", [""])[0]
                    self.send_json(200, {"ok": True, "messages": history.list(direction)})
                elif path.startswith("/api/messages/history/"):
                    item_id = path.rsplit("/", 1)[-1]
                    item = history.get(item_id)
                    if not item:
                        self.send_json(404, {"ok": False, "message": "message not found"})
                        return
                    self.send_json(200, {"ok": True, "message": item})
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
                elif path == "/api/messages/send":
                    self.handle_send_message(data)
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

        def handle_current_messages(self) -> None:
            result = runner.run_cardpulse(["--inbox"])
            payload = result_payload(result)
            messages = [normalize_inbox_entry(entry) for entry in parse_message_blocks(result.output)]
            if result.ok:
                for message in messages:
                    history.add(message)
            payload.update({
                "source": "module",
                "messages": messages,
            })
            self.send_json(200, payload)

        def handle_current_message(self, index: str) -> None:
            if not is_sms_index(index):
                self.send_json(400, {"ok": False, "message": "SMS index must be a single non-negative integer"})
                return
            result = runner.run_cardpulse(["--read-sms", index])
            payload = result_payload(result)
            blocks = parse_message_blocks(result.output)
            if result.ok and blocks:
                message = normalize_sms_message(blocks[0], index=index)
                history.add(message)
                payload["message"] = message
            elif result.ok:
                payload["ok"] = False
                payload["message"] = "SMS read succeeded but no parseable message was returned"
            self.send_json(200, payload)

        def handle_send_message(self, data: dict[str, Any]) -> None:
            if not allow_sms:
                self.send_json(403, {"ok": False, "message": "SMS sending is disabled on this server"})
                return
            if data.get("confirm") != "SEND_SMS":
                self.send_json(400, {"ok": False, "message": "Message send requires confirmation token SEND_SMS"})
                return
            phone = str(data.get("phone", "")).strip()
            message_text = str(data.get("message", "")).strip()
            validation_error = validate_web_sms(phone, message_text)
            if validation_error:
                self.send_json(400, {"ok": False, "message": validation_error})
                return
            result = runner.send_message(phone, message_text)
            payload = result_payload(result)
            sent_at = utc_timestamp()
            outbound = {
                "id": unique_message_id("outbound", phone, sent_at, message_text),
                "index": "",
                "direction": "outbound",
                "from": "",
                "to": phone,
                "phone": phone,
                "time": sent_at,
                "status": "SENT" if result.ok else "FAILED",
                "preview": message_text[:80] + ("..." if len(message_text) > 80 else ""),
                "body": message_text,
                "storage": "history",
                "source": "send",
            }
            if result.ok:
                history.add(outbound)
            payload["message"] = outbound
            self.send_json(200 if result.ok else 500, payload)

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
    history_path = Path(
        os.environ.get(
            "CARDPULSE_WEB_HISTORY_PATH",
            str(Path(os.environ.get("CARDPULSE_STATE_DIR", str(root_dir / "state"))) / "messages.jsonl"),
        )
    )
    runner = CardPulseRunner(root_dir=root_dir)
    handler = make_handler(runner=runner, ui_path=ui_path, allow_sms=allow_sms, history_path=history_path)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}"
    print(f"CardPulse Web listening on {url}")
    if not allow_sms:
        print("SMS sending actions are disabled. Start with --allow-sms to enable guarded SMS endpoints.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
