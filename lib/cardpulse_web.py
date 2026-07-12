#!/usr/bin/env python3
"""Small local Web control surface for CardPulse."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import socketserver
import stat
import subprocess
import threading
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import yaml

from web_auth import Session, SessionStore, verify_password


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 45
WEB_VERSION = "1.1.0"


if hasattr(socketserver, "UnixStreamServer"):
    class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        """Threaded HTTP server for a locally-mounted fnOS gateway socket."""

        daemon_threads = True
        is_unix_socket = True
else:
    class ThreadingUnixHTTPServer(ThreadingHTTPServer):
        """Import-safe placeholder for development hosts without Unix sockets."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("Unix domain sockets are not supported on this host")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CONTROL_RE = re.compile(r"[\r\n\x00-\x1f\x7f]")
MESSAGE_BLOCK_KEYS = {
    "index",
    "indexes",
    "status",
    "from",
    "to",
    "time",
    "parts",
    "preview",
    "message",
}
MESSAGE_RECORD_START_KEYS = {"index", "indexes"}
MESSAGE_BODY_KEYS = {"preview", "message"}
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
HISTORY_DEFAULT_LIMIT = 200
HISTORY_MAX_LIMIT = 500
HISTORY_RETENTION_DAYS = 90
MAX_BATCH_DELETE_SLOTS = 5
QDC507_ACCEPTANCE_DEVICE = "/dev/cardpulse-at"
QDC507_ACCEPTANCE_COMMANDS = ("--doctor", "--info", "--sms-status", "--status")
QDC507_ACCEPTANCE_SCHEMA_VERSION = 3
QDC507_USB_ID = "2ca3:4006"
QDC507_RUNTIME_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
QDC507_IMAGE_REFERENCE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-fA-F]{64}$")
QDC507_RESOLVED_DEVICE_RE = re.compile(r"^/dev/[A-Za-z0-9._-]+$")
QDC507_SYSFS_TTY_ROOT = Path("/sys/class/tty")
QDC507_SYSFS_DEV_RE = re.compile(r"^([0-9]+):([0-9]+)$")
QDC507_USB_COMPONENT_RE = re.compile(r"^[0-9a-fA-F]{4}$")
GATEWAY_CSRF_COOKIE = "cardpulse_gateway_csrf"
FNOS_GATEWAY_BASE_PATH = "/app/cardpulse"
HOST_HEADER_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$"
)

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


def first_parsed_value(parsed: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = parsed.get(key)
        if value:
            return value
    return ""


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


def sms_storage_audit_snapshot(summary: dict[str, Any]) -> dict[str, Any]:
    used = summary.get("storage_used")
    total = summary.get("storage_total")
    return {
        "name": str(summary.get("storage_name", "")),
        "used": used if isinstance(used, int) else None,
        "total": total if isinstance(total, int) else None,
        "remaining": total - used if isinstance(used, int) and isinstance(total, int) else None,
        "full": summary.get("storage_full") if isinstance(summary.get("storage_full"), bool) else None,
        "format": str(summary.get("message_format", "")),
    }


def parse_message_blocks(output: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    last_key = ""
    clean_output = strip_ansi(output)
    inbox_mode = "=== SMS inbox ===" in clean_output
    for raw_line in clean_output.splitlines():
        line = raw_line.rstrip()
        if line.startswith("==="):
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            normalized = key.strip().lower().replace(" ", "_")
            if normalized in MESSAGE_RECORD_START_KEYS and (inbox_mode or not current):
                if current:
                    blocks.append(current)
                current = {normalized: value.strip()}
                last_key = normalized
                continue
        if not current:
            continue
        if last_key in MESSAGE_BODY_KEYS:
            if inbox_mode and ":" in line:
                key, value = line.split(":", 1)
                normalized = key.strip().lower().replace(" ", "_")
                if normalized in {"status", "from", "time", "parts"}:
                    current[normalized] = value.strip()
                    last_key = normalized
                    continue
            current[last_key] = current[last_key] + "\n" + line.strip()
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = key.strip().lower().replace(" ", "_")
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


def fnos_config_directory() -> Optional[Path]:
    config_dir = os.environ.get("CARDPULSE_CONFIG_DIR")
    if os.environ.get("CARDPULSE_FNOS_RUNTIME") == "1" and config_dir:
        return Path(config_dir)
    return None


def fnos_data_directory() -> Optional[Path]:
    data_dir = os.environ.get("CARDPULSE_DATA_DIR")
    if os.environ.get("CARDPULSE_FNOS_RUNTIME") == "1" and data_dir:
        return Path(data_dir)
    return None


def paths_match(path: Path, expected: Path) -> bool:
    return os.path.abspath(path) == os.path.abspath(expected)


def is_fnos_shared_config(path: Path) -> bool:
    config_dir = fnos_config_directory()
    return config_dir is not None and paths_match(Path(path), config_dir / "config.yaml")


def private_directory_mode(path: Path) -> int:
    config_dir = fnos_config_directory()
    if config_dir is not None:
        if paths_match(Path(path), config_dir):
            return 0o2750
        return 0o2770
    return 0o700


def private_file_mode(path: Path) -> int:
    """Keep fnOS config writable by its lifecycle account and runtime group."""
    if is_fnos_shared_config(path):
        return 0o660
    return 0o600


def harden_mode(path: Path, mode: int, *, strict: bool = False) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        if not strict:
            return
        try:
            current_mode = path.stat().st_mode & 0o7777
        except OSError:
            raise exc
        if current_mode != mode:
            raise PermissionError(f"cannot secure {path}") from exc
        return
    if strict and os.name != "nt" and path.stat().st_mode & 0o7777 != mode:
        raise PermissionError(f"cannot secure {path}")


def ensure_private_directory(path: Path, *, strict: bool = False) -> None:
    path.mkdir(parents=True, exist_ok=True)
    harden_mode(path, private_directory_mode(path), strict=strict)


def ensure_gateway_socket_directory(path: Path, *, strict: bool = False) -> None:
    """Keep the fnOS gateway socket reachable only by the package group."""
    path.mkdir(parents=True, exist_ok=True)
    harden_mode(path, 0o3770, strict=strict)


def ensure_private_file(path: Path, *, strict: bool = False) -> None:
    if path.exists():
        harden_mode(path, private_file_mode(path), strict=strict)


def append_private_text(path: Path, content: str) -> None:
    path = Path(path)
    ensure_private_directory(path.parent)
    mode = private_file_mode(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        try:
            os.fchmod(descriptor, mode)
        except (AttributeError, OSError):
            pass
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    ensure_private_file(path)


def atomic_write_private_text(path: Path, content: str) -> None:
    path = Path(path)
    ensure_private_directory(path.parent)
    mode = private_file_mode(path)
    if is_fnos_shared_config(path):
        if path.is_symlink():
            raise PermissionError(f"refusing to follow shared configuration symlink: {path}")
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC | no_follow)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        ensure_private_file(path)
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, mode)
        except (AttributeError, OSError):
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
        ensure_private_file(path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path.exists():
            temporary_path.unlink()


def scheduler_enabled_from_config(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    in_scheduler = False
    for line in lines:
        if re.match(r"^scheduler:\s*$", line):
            in_scheduler = True
            continue
        if in_scheduler and line and not line.startswith((" ", "\t", "#")):
            break
        if in_scheduler:
            match = re.match(r"^\s+enabled:\s*(true|false)\s*$", line, re.IGNORECASE)
            if match:
                return match.group(1).lower() == "true"
    return False


def set_scheduler_enabled(path: Path, enabled: bool) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        content = ""
    block = f"scheduler:\n  enabled: {'true' if enabled else 'false'}\n"
    pattern = re.compile(r"(?m)^scheduler:\n(?:^[ \t]+.*(?:\n|$))*")
    if pattern.search(content):
        content = pattern.sub(block, content, count=1)
    else:
        content = content.rstrip() + "\n\n" + block
    atomic_write_private_text(path, content)


def load_cardpulse_config(path: Path) -> dict[str, Any]:
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("CardPulse configuration cannot be read") from exc
    if not isinstance(config, dict):
        raise ValueError("CardPulse configuration must be a YAML mapping")
    return config


def keepalive_config_error(path: Path) -> str:
    try:
        config = load_cardpulse_config(path)
    except ValueError as exc:
        return str(exc)

    serial = config.get("serial")
    sms = config.get("sms")
    if not isinstance(serial, dict) or serial.get("port") != "/dev/cardpulse-at":
        return "fnOS requires the validated /dev/cardpulse-at device path"
    if serial.get("auto_detect") is not False:
        return "fnOS automatic serial detection must remain disabled"
    if not isinstance(sms, dict):
        return "SMS recipient and message must be configured before enabling the scheduler"

    phone = sms.get("phone")
    message = sms.get("message")
    if not isinstance(phone, str) or not phone.strip():
        return "SMS recipient must be configured before enabling the scheduler"
    if not isinstance(message, str) or not message.strip():
        return "SMS message must be configured before enabling the scheduler"
    validation_error = validate_web_sms(phone.strip(), message.strip())
    if validation_error:
        return validation_error

    interval_days = sms.get("interval_days")
    if isinstance(interval_days, bool) or not isinstance(interval_days, int) or interval_days < 1:
        return "keepalive interval must be a positive integer"
    return ""


def get_keepalive_config(path: Path) -> dict[str, Any]:
    config = load_cardpulse_config(path)
    sms = config.get("sms") if isinstance(config.get("sms"), dict) else {}
    interval_days = sms.get("interval_days", 179)
    if isinstance(interval_days, bool) or not isinstance(interval_days, int):
        interval_days = 179
    reason = keepalive_config_error(path)
    return {
        "phone": str(sms.get("phone", "")),
        "message": str(sms.get("message", "")),
        "interval_days": interval_days,
        "ready": not bool(reason),
        "reason": reason,
    }


def set_keepalive_config(path: Path, phone: str, message: str, interval_days: int) -> dict[str, Any]:
    if isinstance(interval_days, bool) or not isinstance(interval_days, int) or interval_days < 1:
        raise ValueError("keepalive interval must be a positive integer")
    phone = phone.strip()
    message = message.strip()
    validation_error = validate_web_sms(phone, message)
    if validation_error:
        raise ValueError(validation_error)

    config = load_cardpulse_config(path)
    sms = config.setdefault("sms", {})
    if not isinstance(sms, dict):
        raise ValueError("CardPulse SMS configuration must be a mapping")
    sms["phone"] = phone
    sms["message"] = message
    sms["interval_days"] = interval_days
    atomic_write_private_text(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    return get_keepalive_config(path)


def qdc507_fnos_lifecycle_record_status(path: Path) -> tuple[bool, str]:
    """Require an fnOS acceptance record that the runtime cannot replace."""
    if os.environ.get("CARDPULSE_FNOS_RUNTIME") != "1":
        return True, ""

    data_dir = fnos_data_directory()
    if data_dir is None:
        return False, "QDC507 lifecycle acceptance data directory is unavailable"
    lifecycle_dir = data_dir / "lifecycle"
    expected_path = lifecycle_dir / "qdc507-readonly-acceptance.json"
    if not paths_match(path, expected_path):
        return False, "QDC507 lifecycle acceptance record location is invalid"

    try:
        data_stat = data_dir.lstat()
        lifecycle_stat = lifecycle_dir.lstat()
        marker_stat = path.lstat()
        runtime_uid = os.getuid()
    except (AttributeError, OSError):
        return False, "QDC507 lifecycle acceptance record cannot be inspected"

    if (
        not stat.S_ISDIR(data_stat.st_mode)
        or (data_stat.st_mode & 0o7777) != 0o3770
        or not stat.S_ISDIR(lifecycle_stat.st_mode)
        or lifecycle_stat.st_uid != data_stat.st_uid
        or lifecycle_stat.st_gid != data_stat.st_gid
        or lifecycle_stat.st_uid == runtime_uid
        or (lifecycle_stat.st_mode & 0o7777) != 0o2750
    ):
        return False, "QDC507 lifecycle acceptance directory is invalid"
    if (
        not stat.S_ISREG(marker_stat.st_mode)
        or marker_stat.st_uid != lifecycle_stat.st_uid
        or marker_stat.st_gid != lifecycle_stat.st_gid
        or marker_stat.st_uid == runtime_uid
        or (marker_stat.st_mode & 0o777) != 0o640
    ):
        return False, "QDC507 lifecycle acceptance record permissions are invalid"
    return True, ""


def qdc507_readonly_acceptance_status(path: Optional[Path]) -> tuple[bool, str]:
    """Return whether the fnOS QDC507 read-only acceptance record is trustworthy."""
    if path is None:
        return True, ""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, "QDC507 read-only acceptance has not been completed"
    except (OSError, json.JSONDecodeError):
        return False, "QDC507 read-only acceptance record cannot be read"

    lifecycle_record_ok, lifecycle_record_reason = qdc507_fnos_lifecycle_record_status(path)
    if not lifecycle_record_ok:
        return False, lifecycle_record_reason

    if os.name != "nt":
        try:
            marker_mode = path.stat().st_mode & 0o777
        except OSError:
            return False, "QDC507 read-only acceptance record cannot be read"
        expected_marker_mode = 0o640 if fnos_data_directory() is not None else 0o600
        if marker_mode != expected_marker_mode:
            return False, "QDC507 read-only acceptance record permissions are invalid"

    if not isinstance(record, dict):
        return False, "QDC507 read-only acceptance record is invalid"
    if (
        record.get("schema_version") != QDC507_ACCEPTANCE_SCHEMA_VERSION
        or isinstance(record.get("schema_version"), bool)
    ):
        return False, "QDC507 read-only acceptance schema is invalid"
    if record.get("device") != QDC507_ACCEPTANCE_DEVICE:
        return False, "QDC507 read-only acceptance used an unexpected device"
    resolved_device = record.get("resolved_device")
    if not isinstance(resolved_device, str) or not QDC507_RESOLVED_DEVICE_RE.fullmatch(resolved_device):
        return False, "QDC507 read-only acceptance resolved device is invalid"
    if record.get("usb_id") != QDC507_USB_ID:
        return False, "QDC507 read-only acceptance USB identity is invalid"
    runtime_version = record.get("runtime_version")
    if not isinstance(runtime_version, str) or not QDC507_RUNTIME_VERSION_RE.fullmatch(runtime_version):
        return False, "QDC507 read-only acceptance runtime version is invalid"
    image_reference = record.get("image_reference")
    if not isinstance(image_reference, str) or not QDC507_IMAGE_REFERENCE_RE.fullmatch(image_reference):
        return False, "QDC507 read-only acceptance image reference is invalid"
    package_version = record.get("package_version")
    if not isinstance(package_version, str) or not QDC507_RUNTIME_VERSION_RE.fullmatch(package_version):
        return False, "QDC507 read-only acceptance package version is invalid"
    if record.get("commands") != list(QDC507_ACCEPTANCE_COMMANDS):
        return False, "QDC507 read-only acceptance command set is invalid"
    if record.get("nonroot") is not True or record.get("socket_backend") is not True:
        return False, "QDC507 read-only acceptance did not verify required permissions"
    if record.get("device_mode") != "enabled":
        return False, "QDC507 read-only acceptance did not use enabled device mode"

    current_device_ok, current_device_reason = qdc507_current_device_identity(resolved_device)
    if not current_device_ok:
        return False, current_device_reason

    if os.environ.get("CARDPULSE_FNOS_RUNTIME") == "1":
        # The image digest fixes this environment value. A runtime-writable
        # state file must never be allowed to extend a hardware acceptance.
        current_runtime_version = os.environ.get("CARDPULSE_RUNTIME_VERSION", "").strip()
        if not QDC507_RUNTIME_VERSION_RE.fullmatch(current_runtime_version):
            return False, "QDC507 read-only acceptance runtime version is invalid"
        current_runtime_image = os.environ.get("CARDPULSE_RUNTIME_IMAGE", "").strip()
        if not QDC507_IMAGE_REFERENCE_RE.fullmatch(current_runtime_image):
            return False, "QDC507 read-only acceptance runtime image is invalid"
        current_package_version = os.environ.get("CARDPULSE_FPK_VERSION", "").strip()
        if not QDC507_RUNTIME_VERSION_RE.fullmatch(current_package_version):
            return False, "QDC507 read-only acceptance package version is invalid"
    else:
        try:
            current_runtime_version = path.with_name("runtime-version").read_text(encoding="utf-8").strip()
        except OSError:
            return False, "QDC507 read-only acceptance runtime version cannot be read"
    if current_runtime_version != runtime_version:
        return False, "QDC507 read-only acceptance runtime version has changed"
    if os.environ.get("CARDPULSE_FNOS_RUNTIME") == "1":
        if current_runtime_image != image_reference:
            return False, "QDC507 read-only acceptance runtime image has changed"
        if current_package_version != package_version:
            return False, "QDC507 read-only acceptance package version has changed"

    active_device_mode_path = qdc507_active_device_mode_path(path)
    try:
        active_device_mode_stat = active_device_mode_path.lstat()
        if not stat.S_ISREG(active_device_mode_stat.st_mode):
            return False, "QDC507 read-only acceptance device mode is invalid"
        active_device_mode = active_device_mode_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False, "QDC507 read-only acceptance device mode cannot be read"
    if active_device_mode != "enabled":
        return False, "QDC507 read-only acceptance device mode is no longer enabled"

    completed_at = parse_utc_timestamp(record.get("completed_at"))
    if completed_at is None:
        return False, "QDC507 read-only acceptance timestamp is invalid"
    if completed_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        return False, "QDC507 read-only acceptance timestamp is in the future"
    return True, ""


def qdc507_active_device_mode_path(acceptance_path: Path) -> Path:
    """Use the lifecycle-owned mode state when fnOS provides its fixed path."""
    configured_path = os.environ.get("CARDPULSE_QDC507_DEVICE_MODE_PATH", "").strip()
    if configured_path:
        return Path(configured_path)
    return acceptance_path.with_name("qdc507-device-mode.active")


def qdc507_current_device_identity(resolved_device: str) -> tuple[bool, str]:
    """Fail closed unless the current fixed alias still identifies the accepted QDC507."""
    try:
        alias_stat = os.stat(QDC507_ACCEPTANCE_DEVICE)
    except OSError:
        return False, "QDC507 current device alias cannot be read"
    if not stat.S_ISCHR(alias_stat.st_mode):
        return False, "QDC507 current device alias is not a character device"
    if not hasattr(os, "major") or not hasattr(os, "minor"):
        return False, "QDC507 current device identity is unavailable on this host"

    try:
        device_major = os.major(alias_stat.st_rdev)
        device_minor = os.minor(alias_stat.st_rdev)
    except (AttributeError, OSError, ValueError):
        return False, "QDC507 current device alias has invalid device numbers"

    try:
        tty_nodes = list(QDC507_SYSFS_TTY_ROOT.iterdir())
    except OSError:
        return False, "QDC507 current device sysfs cannot be read"

    matching_ttys: list[Path] = []
    for tty_node in tty_nodes:
        try:
            dev_value = (tty_node / "dev").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        dev_match = QDC507_SYSFS_DEV_RE.fullmatch(dev_value)
        if not dev_match:
            continue
        if (int(dev_match.group(1)), int(dev_match.group(2))) == (device_major, device_minor):
            matching_ttys.append(tty_node)

    if not matching_ttys:
        return False, "QDC507 current device alias is absent from tty sysfs"
    if len(matching_ttys) != 1:
        return False, "QDC507 current device alias matches multiple tty sysfs nodes"

    tty_node = matching_ttys[0]
    if f"/dev/{tty_node.name}" != resolved_device:
        return False, "QDC507 current device resolved device does not match acceptance"

    try:
        current_node = tty_node.resolve(strict=True)
    except OSError:
        return False, "QDC507 current device sysfs node cannot be resolved"
    while True:
        vendor_path = current_node / "idVendor"
        product_path = current_node / "idProduct"
        try:
            vendor = vendor_path.read_text(encoding="utf-8").strip().lower()
        except FileNotFoundError:
            vendor = ""
        except OSError:
            return False, "QDC507 current device USB identity cannot be read"
        try:
            product = product_path.read_text(encoding="utf-8").strip().lower()
        except FileNotFoundError:
            product = ""
        except OSError:
            return False, "QDC507 current device USB identity cannot be read"

        if vendor or product:
            if not QDC507_USB_COMPONENT_RE.fullmatch(vendor) or not QDC507_USB_COMPONENT_RE.fullmatch(product):
                return False, "QDC507 current device USB identity is invalid"
            if f"{vendor}:{product}" != QDC507_USB_ID:
                return False, "QDC507 current device USB identity does not match QDC507"
            return True, ""

        parent_node = current_node.parent
        if parent_node == current_node:
            break
        current_node = parent_node

    return False, "QDC507 current device USB identity is unavailable"


def parse_utc_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


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


def parse_sms_indexes(entry: dict[str, str]) -> list[str]:
    if "indexes" in entry:
        values = entry.get("indexes", "").split(",")
    else:
        values = [entry.get("index", "")]
    indexes = [value.strip() for value in values if value.strip()]
    if not indexes or any(not is_sms_index(index) for index in indexes):
        return []
    if len(set(indexes)) != len(indexes):
        return []
    return indexes


def parse_multipart_parts(value: str) -> tuple[int | None, int | None]:
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", value or "")
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def normalize_inbox_entry(entry: dict[str, str]) -> dict[str, Any]:
    indexes = parse_sms_indexes(entry)
    is_multipart = "indexes" in entry or len(indexes) > 1
    parts_received, parts_total = parse_multipart_parts(entry.get("parts", ""))
    multipart_complete = not is_multipart or (
        parts_received == parts_total == len(indexes) and parts_total is not None
    )
    reported_complete = parse_bool(entry.get("complete", ""))
    if is_multipart and reported_complete is not None:
        multipart_complete = multipart_complete and reported_complete
    index = indexes[0] if len(indexes) == 1 and not is_multipart else ""
    sender = entry.get("from", "")
    timestamp = entry.get("time", "")
    preview = entry.get("preview", "").strip()
    status = entry.get("status", "")
    slot_identity = ",".join(indexes)
    return {
        "id": module_message_id("inbound", sender, timestamp, slot_identity),
        "index": index,
        "indexes": indexes,
        "is_multipart": is_multipart,
        "multipart_complete": multipart_complete,
        "physical_slot_count": len(indexes),
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
        "indexes": [index],
        "is_multipart": False,
        "multipart_complete": True,
        "physical_slot_count": 1,
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
    def __init__(
        self,
        path: Path | None = None,
        *,
        now: Any = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.path = Path(path) if path else None
        self._messages: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._now = now
        self._load()

    def _is_retained(self, message: dict[str, Any]) -> bool:
        first_seen_at = parse_utc_timestamp(message.get("first_seen_at"))
        if not first_seen_at:
            return False
        age = self._now().astimezone(timezone.utc) - first_seen_at
        return timedelta(0) <= age < timedelta(days=HISTORY_RETENTION_DAYS)

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        changed = False
        try:
            raw_lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for raw_line in raw_lines:
            if not raw_line.strip():
                continue
            try:
                data = json.loads(raw_line)
            except json.JSONDecodeError:
                changed = True
                continue
            if not isinstance(data, dict) or not data.get("id") or not self._is_retained(data):
                changed = True
                continue
            self._messages[str(data["id"])] = data
        ensure_private_directory(self.path.parent)
        ensure_private_file(self.path)
        if changed:
            self._rewrite()

    def _persist(self, message: dict[str, Any]) -> None:
        if not self.path:
            return
        append_private_text(self.path, json.dumps(message, ensure_ascii=False, sort_keys=True) + "\n")

    def _rewrite(self) -> None:
        if not self.path:
            return
        atomic_write_private_text(
            self.path,
            "".join(json.dumps(message, ensure_ascii=False, sort_keys=True) + "\n" for message in self._messages.values()),
        )

    def add(self, message: dict[str, Any]) -> dict[str, Any]:
        stored = dict(message)
        stored.setdefault(
            "id",
            message_id(
                str(stored.get("direction", "")),
                str(stored.get("phone", "")),
                str(stored.get("time", "")),
                str(stored.get("body", "")),
                str(stored.get("index", "")),
            ),
        )
        stored.setdefault("first_seen_at", self._now().astimezone(timezone.utc).isoformat())
        with self._lock:
            existing = self._messages.get(stored["id"])
            if existing:
                merged = dict(existing)
                merged.update({key: value for key, value in stored.items() if value not in ("", None)})
                merged["first_seen_at"] = existing["first_seen_at"]
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

    def clear(self) -> int:
        with self._lock:
            count = len(self._messages)
            self._messages.clear()
            self._rewrite()
            return count

    def list(self, direction: str = "", limit: int = HISTORY_DEFAULT_LIMIT) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), HISTORY_MAX_LIMIT))
        with self._lock:
            messages = list(self._messages.values())
        if direction in {"inbound", "outbound"}:
            messages = [item for item in messages if item.get("direction") == direction]
        return sorted(
            messages,
            key=lambda item: (str(item.get("time", "")), str(item.get("id", ""))),
            reverse=True,
        )[:limit]

    def get(self, message_id_value: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._messages.get(message_id_value)
            return dict(item) if item else None


def compact_message_record(
    message: dict[str, Any] | None,
    *,
    include_sensitive: bool = True,
) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    msg_id = str(message.get("id", "")).strip()
    if not msg_id:
        return None
    compact = {
        "id": msg_id,
        "index": str(message.get("index", "")),
        "indexes": [str(index) for index in message.get("indexes", []) if str(index)],
        "is_multipart": bool(message.get("is_multipart")),
        "multipart_complete": bool(message.get("multipart_complete", True)),
        "physical_slot_count": int(message.get("physical_slot_count", 0) or 0),
        "direction": str(message.get("direction", "")),
        "time": str(message.get("time", "")),
        "status": str(message.get("status", "")),
        "storage": str(message.get("storage", "")),
        "source": str(message.get("source", "")),
    }
    if include_sensitive:
        compact.update(
            {
                "from": str(message.get("from", "")),
                "to": str(message.get("to", "")),
                "phone": str(message.get("phone", "")),
                "preview": str(message.get("preview") or message.get("body") or "").strip(),
            }
        )
    return compact


def sort_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(messages, key=lambda item: (str(item.get("time", "")), str(item.get("id", ""))))


def latest_message_overall(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    compacted = [compact_message_record(message) for message in messages]
    compacted = [item for item in compacted if item]
    if not compacted:
        return None
    return sort_messages(compacted)[-1]


def latest_message_for_direction(messages: list[dict[str, Any]], direction: str) -> dict[str, Any] | None:
    filtered = [message for message in messages if str(message.get("direction", "")) == direction]
    return latest_message_overall(filtered)


class OpsState:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "inbox_baseline_ready": False,
            "current_message_ids": [],
            "pending_inbound": [],
            "last_storage_alert_level": "",
            "last_storage_alert_summary": "",
            "recent_messages": {
                "last_inbound": None,
                "last_outbound": None,
            },
            "last_failure": None,
        }
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(loaded, dict):
            return
        for key in (
            "inbox_baseline_ready",
            "current_message_ids",
            "pending_inbound",
            "last_storage_alert_level",
            "last_storage_alert_summary",
            "last_failure",
        ):
            if key in loaded:
                self._data[key] = loaded[key]
        if isinstance(loaded.get("recent_messages"), dict):
            self._data["recent_messages"].update(loaded["recent_messages"])
        self._data = self._persistable_data()
        ensure_private_directory(self.path.parent)
        self._persist()

    def _persist(self) -> None:
        if not self.path:
            return
        atomic_write_private_text(
            self.path,
            json.dumps(self._persistable_data(), ensure_ascii=False, indent=2),
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def note_current_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        inbound_messages = [
            compact_message_record(message, include_sensitive=True)
            for message in messages
            if message.get("direction") == "inbound"
        ]
        inbound_messages = [item for item in inbound_messages if item and item.get("id")]
        current_ids = [str(message.get("id", "")) for message in messages if message.get("id")]
        inbound_ids = {item["id"] for item in inbound_messages}

        with self._lock:
            known_ids = set(str(value) for value in self._data.get("current_message_ids", []))
            baseline_ready = bool(self._data.get("inbox_baseline_ready"))
            new_messages: list[dict[str, Any]] = []
            if baseline_ready:
                new_messages = [item for item in inbound_messages if item["id"] not in known_ids]

            pending_map: dict[str, dict[str, Any]] = {}
            for existing in self._data.get("pending_inbound", []):
                if not isinstance(existing, dict):
                    continue
                existing_id = str(existing.get("id", ""))
                if existing_id and existing_id in inbound_ids:
                    pending_map[existing_id] = (
                        compact_message_record(existing, include_sensitive=True) or existing
                    )
            for item in new_messages:
                pending_map[item["id"]] = item

            self._data["inbox_baseline_ready"] = True
            self._data["current_message_ids"] = current_ids
            self._data["pending_inbound"] = sort_messages(list(pending_map.values()))
            if inbound_messages:
                self._data["recent_messages"]["last_inbound"] = latest_message_overall(inbound_messages)
            self._persist()
            return copy.deepcopy(sort_messages(new_messages))

    def note_message(self, message: dict[str, Any]) -> None:
        compact = compact_message_record(message, include_sensitive=True)
        if not compact:
            return
        with self._lock:
            if compact.get("direction") == "inbound":
                self._data["recent_messages"]["last_inbound"] = compact
                pending: list[dict[str, Any]] = []
                for existing in self._data.get("pending_inbound", []):
                    if not isinstance(existing, dict):
                        continue
                    existing_id = str(existing.get("id", ""))
                    existing_index = str(existing.get("index", ""))
                    existing_phone = str(existing.get("phone", ""))
                    same_message = existing_id == compact["id"]
                    if not same_message and existing_index and existing_index == compact["index"]:
                        same_message = not existing_phone or existing_phone == compact["phone"]
                    if not same_message:
                        pending.append(existing)
                self._data["pending_inbound"] = sort_messages(pending)
            elif compact.get("direction") == "outbound":
                self._data["recent_messages"]["last_outbound"] = compact
            self._persist()

    def _persistable_data(self) -> dict[str, Any]:
        data = copy.deepcopy(self._data)
        for key in ("pending_inbound",):
            values = data.get(key, [])
            data[key] = [
                compact_message_record(value, include_sensitive=False)
                for value in values
                if compact_message_record(value, include_sensitive=False)
            ]
        recent_messages = data.get("recent_messages", {})
        if isinstance(recent_messages, dict):
            data["recent_messages"] = {
                key: compact_message_record(value, include_sensitive=False)
                for key, value in recent_messages.items()
            }
        return data

    def clear_pending_inbound(
        self,
        *,
        index: str = "",
        phone: str = "",
        message_id_value: str = "",
    ) -> None:
        index = str(index or "")
        phone = str(phone or "")
        message_id_value = str(message_id_value or "")
        with self._lock:
            pending: list[dict[str, Any]] = []
            for existing in self._data.get("pending_inbound", []):
                if not isinstance(existing, dict):
                    continue
                existing_index = str(existing.get("index", ""))
                existing_phone = str(existing.get("phone", ""))
                existing_id = str(existing.get("id", ""))
                existing_indexes = {
                    str(value)
                    for value in existing.get("indexes", [])
                    if str(value)
                }
                same_message = bool(message_id_value and existing_id == message_id_value)
                if not same_message and index:
                    same_message = existing_index == index or index in existing_indexes
                if same_message and phone:
                    same_message = existing_phone == phone
                if not same_message:
                    pending.append(existing)
            self._data["pending_inbound"] = sort_messages(pending)
            self._persist()

    def note_storage_alert(self, alert: dict[str, str]) -> tuple[str, str] | None:
        level = str(alert.get("level", ""))
        summary = str(alert.get("summary", ""))
        with self._lock:
            previous_level = str(self._data.get("last_storage_alert_level", ""))
            self._data["last_storage_alert_level"] = level
            self._data["last_storage_alert_summary"] = summary
            self._persist()
        if level in {"warn", "danger"} and level != previous_level:
            return ("CardPulse 短信容量告警", summary)
        if level == "ok" and previous_level in {"warn", "danger"}:
            return ("CardPulse 短信容量恢复", summary)
        return None

    def note_failure(self, summary: str, kind: str = "error") -> None:
        with self._lock:
            self._data["last_failure"] = {
                "kind": kind,
                "summary": summary,
                "checked_at": utc_timestamp(),
            }
            self._persist()

    def clear_failure(self, *kinds: str) -> None:
        allowed = {str(kind) for kind in kinds if kind}
        with self._lock:
            current = self._data.get("last_failure")
            if not isinstance(current, dict):
                return
            if allowed and str(current.get("kind", "")) not in allowed:
                return
            self._data["last_failure"] = None
            self._persist()


def load_json_file(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def load_recovery_state(path: Path | None) -> dict[str, Any]:
    loaded = load_json_file(path)
    if not loaded:
        return {
            "state": "",
            "step": "",
            "phase_status": "",
            "summary": "",
            "operator_hint": "",
            "checked_at": "",
            "port": "",
            "web_url": "",
            "busid": "",
            "distro": "",
        }
    return {
        "state": str(loaded.get("state", "")),
        "step": str(loaded.get("step", "")),
        "phase_status": str(loaded.get("phase_status", "")),
        "summary": str(loaded.get("summary", "")),
        "operator_hint": str(loaded.get("operator_hint", "")),
        "checked_at": str(loaded.get("checked_at", "")),
        "port": str(loaded.get("port", "")),
        "web_url": str(loaded.get("web_url", "")),
        "busid": str(loaded.get("busid", "")),
        "distro": str(loaded.get("distro", "")),
    }


def summarize_message_for_notification(message: dict[str, Any]) -> str:
    preview = str(message.get("preview", "") or message.get("body", "") or "").strip()
    lines = [
        f"号码: {message.get('phone') or '-'}",
        f"时间: {message.get('time') or '-'}",
        f"摘要: {preview or '(无内容)'}",
    ]
    return "\n".join(lines)


def compute_storage_alert(sms_status: dict[str, Any]) -> dict[str, str]:
    if not sms_status.get("ok"):
        return {
            "level": "danger",
            "summary": "短信存储读取失败，请检查模块状态后重试。",
        }
    used = sms_status.get("storage_used")
    total = sms_status.get("storage_total")
    if isinstance(used, int) and isinstance(total, int):
        remaining = max(0, total - used)
        label = f"{sms_status.get('storage_name', '')} {used}/{total}".strip()
        if sms_status.get("storage_full") or remaining == 0:
            return {
                "level": "danger",
                "summary": f"短信容量告警：{label} FULL，请删除 1 条旧短信后再接收新短信。",
            }
        if remaining <= 1:
            return {
                "level": "warn",
                "summary": f"短信容量预警：{label}，仅剩 {remaining} 条容量。",
            }
    return {
        "level": "ok",
        "summary": "短信容量正常。",
    }


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
    recent_messages: dict[str, Any] | None = None,
    alerts: dict[str, Any] | None = None,
    recovery: dict[str, Any] | None = None,
    last_failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    recent_messages = recent_messages or {"last_inbound": None, "last_outbound": None}
    alerts = alerts or {
        "new_inbound": {"count": 0, "messages": [], "summary": ""},
        "storage": compute_storage_alert(sms_status),
    }
    recovery = recovery or {}
    last_failure = last_failure if isinstance(last_failure, dict) else None

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
        elif remaining <= 1:
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
        keepalive_summary = "今天应发送保号短信。"
    elif last_result and last_result != "success":
        keepalive_state = "warn"
        keepalive_summary = f"上次保号结果异常：{last_result}"
    elif isinstance(remaining_days, int):
        keepalive_state = "ok"
        keepalive_summary = f"保号正常，距离下次发送还有 {remaining_days} 天。"
    else:
        keepalive_state = "warn"
        keepalive_summary = "保号状态未知，请先运行一次状态检查。"

    if keepalive_state == "ok" and isinstance(remaining_days, int):
        keepalive_summary = f"保号正常，距离下次发送还有 {remaining_days} 天"

    recovery_state = str(recovery.get("state", ""))
    recovery_summary = str(recovery.get("summary", ""))
    recovery_severity = "ok" if recovery_state == "ok" else ("warn" if recovery_state else "ok")

    component_states = [
        connection["state"],
        "ok" if sim["ready"] else "danger",
        signal["state"],
        "ok" if registration["registered"] else "danger",
        storage_severity,
        keepalive_state,
        recovery_severity,
    ]
    overall_status = strongest_severity(component_states)

    command_failures = []
    if not info_ok:
        command_failures.append("模组信息")
    if not status_ok:
        command_failures.append("保号状态")
    if not sms_status_ok:
        command_failures.append("短信存储")

    new_inbound = alerts.get("new_inbound", {}) if isinstance(alerts.get("new_inbound"), dict) else {}
    new_inbound_count = int(new_inbound.get("count", 0) or 0)
    storage_alert = alerts.get("storage", {}) if isinstance(alerts.get("storage"), dict) else {}

    recommended_action = "状态正常，继续保持本地 Web 只读运维即可。"
    if command_failures:
        recommended_action = " / ".join(command_failures) + "读取失败，请查看原始输出并重新运行 WSL 恢复脚本。"
    elif connection["state"] != "ok":
        recommended_action = "设备端口不可用，请重新运行 WSL 恢复脚本。"
    elif not sim["ready"]:
        recommended_action = "SIM 未 READY，请检查 SIM 卡和模块状态。"
    elif signal["state"] != "ok" or not registration["registered"]:
        recommended_action = "网络未稳定注册，请检查信号、天线或运营商状态。"
    elif recovery_state and recovery_state != "ok":
        recommended_action = recovery_summary or "最近恢复状态异常，请重新执行恢复脚本。"
    elif storage_alert.get("level") == "danger":
        recommended_action = str(storage_alert.get("summary") or "短信存储已满，请读取收件箱并删除 1 条旧短信后再接收新短信。")
    elif new_inbound_count > 0:
        recommended_action = f"发现 {new_inbound_count} 条新短信提醒，请先进入消息中心查看。"
    elif storage_alert.get("level") == "warn":
        recommended_action = str(storage_alert.get("summary") or "短信存储接近满仓，建议清理明确无用的旧短信。")
    elif keepalive_state != "ok":
        recommended_action = keepalive_summary
    elif last_failure and last_failure.get("summary"):
        recommended_action = str(last_failure.get("summary"))

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
        "recent_messages": {
            "last_inbound": recent_messages.get("last_inbound"),
            "last_outbound": recent_messages.get("last_outbound"),
        },
        "alerts": alerts,
        "recovery": recovery,
        "last_failure": last_failure,
        "raw": raw,
        "output": "\n\n".join(raw_blocks),
    }


def result_payload(result: CommandResult, *, parsed: Optional[dict[str, str]] = None) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "output": result.output,
        "parsed": parsed if parsed is not None else parse_colon_lines(result.stdout),
    }


def info_summary(parsed: dict[str, str]) -> dict[str, str]:
    return {
        "device": first_parsed_value(parsed, ("device", "设备")),
        "baudrate": first_parsed_value(parsed, ("baudrate", "波特率")),
        "vendor": first_parsed_value(parsed, ("vendor", "厂商")),
        "model": first_parsed_value(parsed, ("model", "型号")),
        "imei": first_parsed_value(parsed, ("imei",)),
        "firmware": first_parsed_value(parsed, ("firmware", "version", "版本")),
        "sim": first_parsed_value(parsed, ("sim", "sim_card", "sim_卡")),
        "signal": first_parsed_value(parsed, ("signal", "rssi", "信号强度")),
        "network": first_parsed_value(parsed, ("network", "network_registration", "网络状态")),
        "operator": first_parsed_value(parsed, ("operator", "运营商")),
    }


def is_readonly_at_command(cmd: str) -> bool:
    normalized = (cmd or "").strip().upper()
    if not normalized or CONTROL_RE.search(normalized):
        return False
    return normalized in READONLY_AT_COMMANDS


def is_sms_index(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9]+", value) is not None


def parse_inbox_records(stdout: str) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    entries = parse_message_blocks(stdout)
    return entries, [normalize_inbox_entry(entry) for entry in entries]


def collect_current_sms_indexes(messages: list[dict[str, Any]]) -> tuple[set[str], str]:
    current_indexes: set[str] = set()
    for message in messages:
        indexes = [str(index) for index in message.get("indexes", [])]
        if (
            not indexes
            or len(indexes) != len(set(indexes))
            or current_indexes.intersection(indexes)
        ):
            return set(), "SMS inbox contains invalid or duplicated indexes; refresh and retry"
        current_indexes.update(indexes)
    return current_indexes, ""


def parse_verified_inbox_indexes(stdout: str) -> tuple[set[str], str]:
    clean_output = strip_ansi(stdout)
    lines = [line.strip() for line in clean_output.splitlines() if line.strip()]
    if "=== SMS inbox ===" not in lines:
        return set(), "SMS inbox reread did not contain the expected inbox contract"

    entries, messages = parse_inbox_records(stdout)
    current_indexes, parse_error = collect_current_sms_indexes(messages)
    if parse_error:
        return set(), parse_error
    if entries:
        return current_indexes, ""

    allowed_empty_lines = {"=== SMS inbox ===", "No SMS messages found."}
    if any(line not in allowed_empty_lines for line in lines):
        return set(), "SMS inbox reread could not be parsed as an empty inbox"
    return set(), ""


def sms_storage_summary_is_strict(summary: dict[str, Any]) -> bool:
    used = summary.get("storage_used")
    total = summary.get("storage_total")
    return (
        bool(summary.get("storage_name"))
        and isinstance(used, int)
        and isinstance(total, int)
        and total > 0
        and 0 <= used <= total
        and isinstance(summary.get("storage_full"), bool)
    )


def strict_sms_timestamp(value: str) -> datetime | None:
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def validate_batch_indexes(value: Any) -> tuple[list[str], str]:
    if not isinstance(value, list) or not value:
        return [], "indexes must be a non-empty array"
    indexes = [item.strip() for item in value if isinstance(item, str)]
    if len(indexes) != len(value) or any(not is_sms_index(index) for index in indexes):
        return [], "SMS indexes must be non-negative integer strings"
    if len(set(indexes)) != len(indexes):
        return [], "SMS indexes must not contain duplicates"
    if len(indexes) > MAX_BATCH_DELETE_SLOTS:
        return [], f"batch deletion supports at most {MAX_BATCH_DELETE_SLOTS} physical SMS slots"
    return indexes, ""


class CardPulseRunner:
    def __init__(
        self,
        root_dir: Path = ROOT_DIR,
        timeout: int = DEFAULT_TIMEOUT,
        extra_env: Optional[dict[str, str]] = None,
    ) -> None:
        self.root_dir = Path(root_dir)
        self.timeout = timeout
        self.extra_env = extra_env or {}
        self._device_lock = threading.Lock()

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.extra_env)
        env.setdefault("CARDPULSE_LIB_DIR", str(self.root_dir / "lib"))
        return env

    def _run_device_subprocess(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        with self._device_lock:
            return subprocess.run(command, **kwargs)

    def run_cardpulse(self, args: list[str]) -> CommandResult:
        configured_bin = os.environ.get("CARDPULSE_WEB_CARDPULSE_BIN", "").strip()
        if configured_bin:
            command = [configured_bin, *args]
        else:
            command = ["bash", str(self.root_dir / "bin" / "cardpulse"), *args]

        completed = self._run_device_subprocess(
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

        completed = self._run_device_subprocess(
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

        completed = self._run_device_subprocess(
            ["bash", "-c", script],
            cwd=str(self.root_dir),
            env=env,
            text=True,
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def notify_event(self, title: str, body: str) -> CommandResult:
        script = r'''
set -euo pipefail
ROOT_DIR="${CARDPULSE_ROOT_DIR:?}"
source "$ROOT_DIR/lib/config_reader.sh"
source "$ROOT_DIR/lib/notifier.sh"
config_init
enabled="$(config_read ".notify.enabled" "false")"
if ! config_is_true "$enabled"; then
    echo "[INFO] notification disabled"
    exit 0
fi
msg="${CARDPULSE_WEB_NOTIFY_TITLE}"$'\n'"${CARDPULSE_WEB_NOTIFY_BODY}"
notify_telegram "$msg" || true
notify_wechat "$msg" || true
notify_wecom "$msg" || true
notify_qq "$msg" || true
notify_feishu "$msg" || true
notify_dingtalk "$msg" || true
notify_bark "$msg" || true
notify_email "$CARDPULSE_WEB_NOTIFY_TITLE" "CardPulse Web" "+000000000000" "$CARDPULSE_WEB_NOTIFY_BODY" || true
printf '%s\n' "$msg"
'''
        env = self._env()
        env["CARDPULSE_ROOT_DIR"] = str(self.root_dir)
        env["CARDPULSE_WEB_NOTIFY_TITLE"] = title
        env["CARDPULSE_WEB_NOTIFY_BODY"] = body

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


class SmsOperationsAudit:
    def __init__(self, audit_path: Path | None) -> None:
        self.audit_path = Path(audit_path) if audit_path else None
        self._lock = threading.Lock()

    def record(
        self,
        *,
        operation: str,
        ok: bool,
        message: str,
        requested_count: int = 0,
        command_succeeded_count: int = 0,
        verified: bool = False,
        verification_error: str = "",
        storage: dict[str, Any] | None = None,
    ) -> None:
        if not self.audit_path:
            return
        audit = {
            "at": utc_timestamp(),
            "source": "web",
            "operation": operation,
            "ok": ok,
            "message": message,
            "requested_count": max(0, int(requested_count)),
            "command_succeeded_count": max(0, int(command_succeeded_count)),
            "verified": bool(verified),
            "verification_error": str(verification_error),
        }
        if storage is not None:
            audit["storage"] = storage
        with self._lock:
            append_private_text(self.audit_path, json.dumps(audit, ensure_ascii=False, sort_keys=True) + "\n")


class LocalHistoryAudit:
    def __init__(self, audit_path: Path | None) -> None:
        self.audit_path = Path(audit_path) if audit_path else None
        self._lock = threading.Lock()

    def record_clear(self, *, cleared_count: int, ok: bool) -> None:
        if not self.audit_path:
            return
        record = {
            "at": utc_timestamp(),
            "operation": "clear_local_history",
            "ok": bool(ok),
            "cleared_count": max(0, int(cleared_count)),
        }
        with self._lock:
            append_private_text(self.audit_path, json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def record_startup_sms_storage_baseline(
    runner: CardPulseRunner,
    operations_audit: SmsOperationsAudit,
) -> None:
    try:
        result = runner.run_cardpulse(["--sms-status"])
    except subprocess.TimeoutExpired:
        operations_audit.record(
            operation="startup_storage_baseline",
            ok=False,
            message="SMS storage baseline check timed out",
        )
        return

    payload = result_payload(result)
    summary = parse_sms_status_summary(payload["parsed"])
    operations_audit.record(
        operation="startup_storage_baseline",
        ok=result.ok,
        message="SMS storage baseline captured" if result.ok else "SMS storage baseline check failed",
        verified=result.ok,
        storage=sms_storage_audit_snapshot(summary),
    )


def verify_sms_slots_deleted(
    runner: CardPulseRunner,
    requested_indexes: list[str],
) -> tuple[int, dict[str, Any]]:
    try:
        inbox_result = runner.run_cardpulse(["--inbox"])
        storage_result = runner.run_cardpulse(["--sms-status"])
    except subprocess.TimeoutExpired:
        return 504, {
            "verified": False,
            "remaining_indexes": [],
            "storage": {},
            "verification_error": "SMS deletion verification timed out",
        }

    storage_payload = result_payload(storage_result)
    storage_summary = parse_sms_status_summary(storage_payload["parsed"])
    verification = {
        "verified": False,
        "remaining_indexes": [],
        "storage": sms_storage_audit_snapshot(storage_summary),
        "verification_error": "",
    }
    if not inbox_result.ok:
        verification["verification_error"] = "SMS inbox reread failed after deletion"
        return 502, verification
    if not storage_result.ok:
        verification["verification_error"] = "SMS storage reread failed after deletion"
        return 502, verification

    current_indexes, parse_error = parse_verified_inbox_indexes(inbox_result.stdout)
    if parse_error:
        verification["verification_error"] = parse_error
        return 409, verification
    if not sms_storage_summary_is_strict(storage_summary):
        verification["verification_error"] = "SMS storage reread could not be parsed"
        return 409, verification

    requested_set = {str(index) for index in requested_indexes}
    remaining_indexes = sorted(requested_set.intersection(current_indexes), key=int)
    verification["remaining_indexes"] = remaining_indexes
    if remaining_indexes:
        verification["verification_error"] = "one or more deleted SMS indexes are still present"
        return 409, verification

    verification["verified"] = True
    return 200, verification


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
    ops_state_path: Optional[Path] = None,
    recovery_state_path: Optional[Path] = None,
    operations_audit_path: Optional[Path] = None,
    history_audit_path: Optional[Path] = None,
    auth_path: Optional[Path] = None,
    public_origin: str = "",
    base_path: str = "",
    gateway_admin_only: bool = False,
    scheduler_config_path: Optional[Path] = None,
    qdc507_acceptance_path: Optional[Path] = None,
) -> type[BaseHTTPRequestHandler]:
    history = MessageHistory(history_path)
    ops_state = OpsState(ops_state_path)
    operation_lock = threading.RLock()
    operations_audit = SmsOperationsAudit(operations_audit_path)
    operations_audit_service = operations_audit
    history_audit = LocalHistoryAudit(history_audit_path)
    auth_config_path = Path(auth_path) if auth_path else None
    session_store = SessionStore()
    configured_public_origin = public_origin.rstrip("/")
    configured_base_path = "/" + base_path.strip("/") if base_path.strip("/") else ""
    scheduler_path = Path(scheduler_config_path) if scheduler_config_path else None
    qdc507_marker_path = Path(qdc507_acceptance_path) if qdc507_acceptance_path else None
    started_at = utc_timestamp()
    service_mode = os.environ.get("CARDPULSE_SERVICE_MODE", "diagnostic")
    state_directory = str(ops_state_path.parent) if ops_state_path else ""

    def safe_notify_event(title: str, body: str) -> None:
        notify = getattr(runner, "notify_event", None)
        if not callable(notify):
            return
        try:
            notify(title, body)
        except Exception:
            return

    def build_new_inbound_alert(snapshot: dict[str, Any]) -> dict[str, Any]:
        messages = snapshot.get("pending_inbound", [])
        if not isinstance(messages, list):
            messages = []
        count = len(messages)
        summary = f"发现 {count} 条新短信提醒" if count else ""
        return {
            "count": count,
            "messages": messages,
            "summary": summary,
        }

    def notify_new_messages(messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        if len(messages) == 1:
            body = summarize_message_for_notification(messages[0])
        else:
            lines = [f"共 {len(messages)} 条新短信"]
            for message in messages[:3]:
                lines.append(summarize_message_for_notification(message))
            if len(messages) > 3:
                lines.append(f"还有 {len(messages) - 3} 条未展开")
            body = "\n\n".join(lines)
        safe_notify_event("CardPulse 新短信提醒", body)

    class CardPulseWebHandler(BaseHTTPRequestHandler):
        server_version = "CardPulseWeb/0.1"
        operations_audit = operations_audit_service

        def log_message(self, fmt: str, *args: object) -> None:
            if os.environ.get("CARDPULSE_WEB_ACCESS_LOG") == "1":
                super().log_message(fmt, *args)

        def send_security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if gateway_admin_only:
                # fnOS serves the package UI from its own same-origin iframe.
                self.send_header("X-Frame-Options", "SAMEORIGIN")
                self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
            else:
                self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")

        def send_json(
            self,
            status: int,
            payload: dict[str, Any],
            *,
            cookies: list[str] | None = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_security_headers()
            for cookie in cookies or []:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_html(self) -> None:
            if ui_path and ui_path.exists():
                body = ui_path.read_bytes()
            else:
                body = DEFAULT_HTML.encode("utf-8")
            body = body.replace(b"__CARDPULSE_BASE_PATH__", configured_base_path.encode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_security_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authentication_enabled(self) -> bool:
            return auth_config_path is not None and not gateway_admin_only

        def gateway_access_allowed(self) -> bool:
            if not gateway_admin_only:
                return True
            return self.headers.get("X-Trim-Isadmin", "").strip().lower() == "true"

        def routed_path(self, raw_path: str) -> str | None:
            if not configured_base_path:
                return raw_path
            if raw_path == configured_base_path:
                return "/"
            if raw_path.startswith(configured_base_path + "/"):
                return raw_path[len(configured_base_path) :]
            return None

        def request_source(self) -> str:
            source = self.client_address[0]
            if source in {"127.0.0.1", "::1"}:
                forwarded_for = self.headers.get("X-Forwarded-For", "")
                if forwarded_for:
                    forwarded_source = forwarded_for.split(",", 1)[0].strip()
                    if forwarded_source:
                        return forwarded_source
            return source

        def request_cookie(self, name: str) -> str:
            raw_cookie = self.headers.get("Cookie", "")
            if not raw_cookie:
                return ""
            try:
                cookies = SimpleCookie()
                cookies.load(raw_cookie)
                morsel = cookies.get(name)
            except (KeyError, ValueError):
                return ""
            return morsel.value if morsel else ""

        def session_from_request(self) -> Session | None:
            if not self.authentication_enabled():
                return None
            token = self.request_cookie("cardpulse_session")
            if not token:
                return None
            session = session_store.get_session(token)
            if not session or session.source != self.request_source():
                return None
            return session

        def validated_origin_host(self, value: str) -> str:
            host = value.strip()
            if host != value or not HOST_HEADER_RE.fullmatch(host):
                return ""
            try:
                parsed = urlparse(f"//{host}")
                port = parsed.port
            except ValueError:
                return ""
            if not parsed.hostname or (port is not None and not 1 <= port <= 65535):
                return ""
            return host.lower()

        def expected_request_origin(self) -> str:
            if gateway_admin_only:
                forwarded_proto = self.headers.get("X-Forwarded-Proto", "")
                forwarded_host = self.headers.get("X-Forwarded-Host", "")
                # fnOS gateway POSTs must be HTTPS and identify the browser-facing host.
                # The socket backend is not a browser origin and must never fall back to Host.
                if not self.gateway_access_allowed() or forwarded_proto.strip().lower() != "https":
                    return ""
                normalized_host = self.validated_origin_host(forwarded_host)
                return f"https://{normalized_host}" if normalized_host else ""

            if configured_public_origin:
                return configured_public_origin

            host = self.headers.get("Host", "")
            normalized_host = self.validated_origin_host(host)
            return f"http://{normalized_host}" if normalized_host else ""

        def origin_is_allowed(self) -> bool:
            origin = self.headers.get("Origin", "").rstrip("/")
            if not origin:
                return False
            expected_origin = self.expected_request_origin()
            return bool(expected_origin) and secrets.compare_digest(origin, expected_origin)

        def require_session(self) -> Session | None:
            if not self.authentication_enabled():
                return None
            session = self.session_from_request()
            if not session:
                self.send_json(401, {"ok": False, "message": "login required"})
                return None
            return session

        def require_authenticated_post(self) -> Session | None:
            session = self.require_session()
            if not self.authentication_enabled():
                return None
            if not session:
                return None
            if not self.origin_is_allowed():
                self.send_json(403, {"ok": False, "message": "Origin is not allowed"})
                return None
            csrf_token = self.headers.get("X-CardPulse-CSRF", "")
            if not csrf_token or not secrets.compare_digest(csrf_token, session.csrf_token):
                self.send_json(403, {"ok": False, "message": "CSRF token is invalid or missing"})
                return None
            return session

        def require_gateway_post_csrf(self) -> bool:
            if not gateway_admin_only:
                return True
            csrf_token = self.headers.get("X-CardPulse-CSRF", "")
            csrf_cookie = self.request_cookie(GATEWAY_CSRF_COOKIE)
            if (
                not csrf_token
                or not csrf_cookie
                or not secrets.compare_digest(csrf_token, csrf_cookie)
            ):
                self.send_json(403, {"ok": False, "message": "CSRF token is invalid or missing"})
                return False
            if not self.origin_is_allowed():
                self.send_json(403, {"ok": False, "message": "Origin is not allowed"})
                return False
            return True

        def session_cookie(self, token: str) -> str:
            return (
                f"cardpulse_session={token}; Path={configured_base_path or '/'}; Max-Age=43200; "
                "HttpOnly; Secure; SameSite=Strict"
            )

        def login_csrf_cookie(self, token: str) -> str:
            return (
                f"cardpulse_login_csrf={token}; Path={configured_base_path or '/'}; Max-Age=600; "
                "Secure; SameSite=Strict"
            )

        def gateway_csrf_cookie(self, token: str) -> str:
            return (
                f"{GATEWAY_CSRF_COOKIE}={token}; Path={configured_base_path or '/'}; Max-Age=43200; "
                "Secure; SameSite=Strict"
            )

        def expired_session_cookie(self) -> str:
            return (
                f"cardpulse_session=; Path={configured_base_path or '/'}; Max-Age=0; "
                "HttpOnly; Secure; SameSite=Strict"
            )

        def handle_auth_session(self) -> None:
            if gateway_admin_only:
                csrf_token = secrets.token_urlsafe(32)
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "authenticated": True,
                        "auth_required": False,
                        "csrf_token": csrf_token,
                    },
                    cookies=[self.gateway_csrf_cookie(csrf_token)],
                )
                return
            if not self.authentication_enabled():
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "authenticated": True,
                        "auth_required": False,
                        "csrf_token": "",
                    },
                )
                return
            session = self.session_from_request()
            if session:
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "authenticated": True,
                        "auth_required": True,
                        "csrf_token": session.csrf_token,
                    },
                )
                return
            login_csrf_token = session_store.issue_login_csrf(self.request_source())
            self.send_json(
                200,
                {
                    "ok": True,
                    "authenticated": False,
                    "auth_required": True,
                    "configured": bool(auth_config_path and auth_config_path.exists()),
                    "login_csrf_token": login_csrf_token,
                },
                cookies=[self.login_csrf_cookie(login_csrf_token)],
            )

        def handle_login(self, data: dict[str, Any]) -> None:
            if not self.authentication_enabled():
                self.send_json(404, {"ok": False, "message": "web authentication is not configured"})
                return
            if not self.origin_is_allowed():
                self.send_json(403, {"ok": False, "message": "Origin is not allowed"})
                return
            source = self.request_source()
            if session_store.is_login_locked(source):
                self.send_json(429, {"ok": False, "message": "too many login attempts; try again later"})
                return
            login_csrf_token = self.headers.get("X-CardPulse-CSRF", "")
            if (
                not login_csrf_token
                or not secrets.compare_digest(
                    login_csrf_token,
                    self.request_cookie("cardpulse_login_csrf"),
                )
                or not session_store.consume_login_csrf(source, login_csrf_token)
            ):
                self.send_json(403, {"ok": False, "message": "CSRF token is invalid or missing"})
                return
            password = data.get("password")
            if not isinstance(password, str) or not auth_config_path or not verify_password(auth_config_path, password):
                locked = session_store.record_login_failure(source)
                self.send_json(
                    429 if locked else 401,
                    {
                        "ok": False,
                        "message": (
                            "too many login attempts; try again later"
                            if locked
                            else "invalid password or missing password configuration"
                        ),
                    },
                )
                return
            session_store.clear_login_failures(source)
            session = session_store.create_session(source)
            self.send_json(
                200,
                {
                    "ok": True,
                    "authenticated": True,
                    "csrf_token": session.csrf_token,
                },
                cookies=[self.session_cookie(session.token)],
            )

        def handle_logout(self, session: Session) -> None:
            session_store.delete_session(session.token)
            self.send_json(
                200,
                {"ok": True, "authenticated": False},
                cookies=[self.expired_session_cookie()],
            )

        def handle_scheduler(self, data: Optional[dict[str, Any]] = None) -> None:
            if not scheduler_path:
                self.send_json(404, {"ok": False, "message": "scheduler controls are unavailable"})
                return
            config_reason = keepalive_config_error(scheduler_path)
            accepted, acceptance_reason = qdc507_readonly_acceptance_status(qdc507_marker_path)
            reason = config_reason or acceptance_reason
            if data is None:
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "enabled": scheduler_enabled_from_config(scheduler_path),
                        "ready": not bool(reason),
                        "accepted": accepted,
                        "reason": reason,
                    },
                )
                return
            enabled = data.get("enabled")
            if not isinstance(enabled, bool) or data.get("confirm") != "SET_SCHEDULER_ENABLED":
                self.send_json(400, {"ok": False, "message": "explicit scheduler confirmation is required"})
                return
            if enabled:
                if reason:
                    self.send_json(
                        409,
                        {"ok": False, "enabled": False, "accepted": accepted, "message": reason},
                    )
                    return
            set_scheduler_enabled(scheduler_path, enabled)
            self.send_json(
                200,
                {
                    "ok": True,
                    "enabled": enabled,
                    "ready": not bool(reason),
                    "accepted": accepted,
                    "reason": reason,
                },
            )

        def handle_keepalive_config(self, data: Optional[dict[str, Any]] = None) -> None:
            if not scheduler_path:
                self.send_json(404, {"ok": False, "message": "keepalive configuration is unavailable"})
                return
            try:
                if data is None:
                    self.send_json(200, {"ok": True, **get_keepalive_config(scheduler_path)})
                    return
                phone = data.get("phone")
                message = data.get("message")
                interval_days = data.get("interval_days")
                if not isinstance(phone, str) or not isinstance(message, str):
                    self.send_json(400, {"ok": False, "message": "SMS recipient and message are required"})
                    return
                if isinstance(interval_days, bool) or not isinstance(interval_days, int):
                    self.send_json(400, {"ok": False, "message": "keepalive interval must be an integer"})
                    return
                self.send_json(200, {"ok": True, **set_keepalive_config(scheduler_path, phone, message, interval_days)})
            except ValueError as exc:
                self.send_json(400, {"ok": False, "message": str(exc)})

        def do_GET(self) -> None:  # noqa: N802
            parsed_url = urlparse(self.path)
            path = self.routed_path(parsed_url.path)
            if path is None:
                self.send_json(404, {"ok": False, "message": "not found"})
                return
            if not self.gateway_access_allowed():
                self.send_json(403, {"ok": False, "message": "fnOS administrator access required"})
                return
            try:
                with operation_lock:
                    if path in ("", "/"):
                        self.send_html()
                    elif path == "/api/health":
                        payload = {
                            "status": "ok",
                            "sms_enabled": allow_sms,
                            "auth_required": self.authentication_enabled(),
                        }
                        session = self.session_from_request()
                        if session or not self.authentication_enabled():
                            payload.update(
                                {
                                    "authenticated": bool(session) or not self.authentication_enabled(),
                                    "version": WEB_VERSION,
                                    "started_at": started_at,
                                    "state_dir": state_directory,
                                    "service_mode": service_mode,
                                }
                            )
                        self.send_json(200, payload)
                    elif path == "/api/auth/session":
                        self.handle_auth_session()
                    elif self.authentication_enabled() and not self.require_session():
                        return
                    elif path == "/api/keepalive-config":
                        self.handle_keepalive_config()
                    elif path == "/api/scheduler":
                        self.handle_scheduler()
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
                        self.handle_overview()
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
                        try:
                            limit = int(params.get("limit", [str(HISTORY_DEFAULT_LIMIT)])[0])
                        except ValueError:
                            limit = HISTORY_DEFAULT_LIMIT
                        limit = max(1, min(limit, HISTORY_MAX_LIMIT))
                        self.send_json(200, {"ok": True, "messages": history.list(direction, limit)})
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
            except Exception as exc:
                self.send_json(500, {"ok": False, "message": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            path = self.routed_path(urlparse(self.path).path)
            if path is None:
                self.send_json(404, {"ok": False, "message": "not found"})
                return
            if not self.gateway_access_allowed():
                self.send_json(403, {"ok": False, "message": "fnOS administrator access required"})
                return
            if not self.require_gateway_post_csrf():
                return
            try:
                data = read_json(self)
                with operation_lock:
                    if path == "/api/auth/login":
                        self.handle_login(data)
                    elif self.authentication_enabled():
                        session = self.require_authenticated_post()
                        if not session:
                            return
                        if path == "/api/auth/logout":
                            self.handle_logout(session)
                        elif path == "/api/actions/test-sms":
                            self.handle_test_sms(data)
                        elif path == "/api/at":
                            self.handle_at(data)
                        elif path == "/api/sms/delete":
                            self.handle_delete_sms(data)
                        elif path == "/api/sms/delete-batch":
                            self.handle_delete_sms_batch(data)
                        elif path == "/api/sms/delete-incomplete":
                            self.handle_delete_incomplete_sms(data)
                        elif path == "/api/messages/current/ack":
                            self.handle_acknowledge_current_message(data)
                        elif path == "/api/messages/history/clear":
                            self.handle_clear_local_history(data)
                        elif path == "/api/messages/send":
                            self.handle_send_message(data)
                        elif path == "/api/keepalive-config":
                            self.handle_keepalive_config(data)
                        elif path == "/api/scheduler":
                            self.handle_scheduler(data)
                        else:
                            self.send_json(404, {"ok": False, "message": "not found"})
                    elif path == "/api/actions/test-sms":
                        self.handle_test_sms(data)
                    elif path == "/api/at":
                        self.handle_at(data)
                    elif path == "/api/sms/delete":
                        self.handle_delete_sms(data)
                    elif path == "/api/sms/delete-batch":
                        self.handle_delete_sms_batch(data)
                    elif path == "/api/sms/delete-incomplete":
                        self.handle_delete_incomplete_sms(data)
                    elif path == "/api/messages/current/ack":
                        self.handle_acknowledge_current_message(data)
                    elif path == "/api/messages/history/clear":
                        self.handle_clear_local_history(data)
                    elif path == "/api/messages/send":
                        self.handle_send_message(data)
                    elif path == "/api/keepalive-config":
                        self.handle_keepalive_config(data)
                    elif path == "/api/scheduler":
                        self.handle_scheduler(data)
                    else:
                        self.send_json(404, {"ok": False, "message": "not found"})
            except ValueError as exc:
                self.send_json(400, {"ok": False, "message": str(exc)})
            except subprocess.TimeoutExpired:
                self.send_json(504, {"ok": False, "message": "command timed out"})
            except Exception as exc:
                self.send_json(500, {"ok": False, "message": str(exc)})

        def handle_overview(self) -> None:
            info_payload = result_payload(runner.run_cardpulse(["--info"]))
            info_payload.update(info_summary(info_payload["parsed"]))

            status_payload = result_payload(runner.run_cardpulse(["--status"]))
            status_payload.update(parse_status_summary(status_payload["parsed"]))

            sms_payload = result_payload(runner.run_cardpulse(["--sms-status"]))
            sms_payload.update(parse_sms_status_summary(sms_payload["parsed"]))

            failures = []
            if not info_payload["ok"]:
                failures.append("模块信息读取失败")
            if not status_payload["ok"]:
                failures.append("保号状态读取失败")
            if not sms_payload["ok"]:
                failures.append("短信存储读取失败")
            if failures:
                ops_state.note_failure(" / ".join(failures), kind="overview")
            else:
                ops_state.clear_failure("overview")

            storage_alert = compute_storage_alert(sms_payload)
            storage_notification = ops_state.note_storage_alert(storage_alert)
            if storage_notification:
                safe_notify_event(*storage_notification)

            snapshot = ops_state.snapshot()
            alerts = {
                "new_inbound": build_new_inbound_alert(snapshot),
                "storage": storage_alert,
            }
            recovery = load_recovery_state(recovery_state_path)

            payload = build_overview_payload(
                info=info_payload,
                status=status_payload,
                sms_status=sms_payload,
                sms_enabled=allow_sms,
                recent_messages=snapshot.get("recent_messages", {}),
                alerts=alerts,
                recovery=recovery,
                last_failure=snapshot.get("last_failure"),
            )
            self.send_json(200, payload)

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
            payload = result_payload(runner.run_cardpulse(["--delete-sms", index, "--confirm", "DELETE_SMS"]))
            if not payload["ok"]:
                payload.update(
                    {
                        "verified": False,
                        "requested_indexes": [index],
                        "command_succeeded_indexes": [],
                    }
                )
                operations_audit.record(
                    operation="manual_single_delete",
                    ok=False,
                    message="manual SMS deletion command failed",
                    requested_count=1,
                )
                self.send_json(500, payload)
                return

            verification_status, verification = verify_sms_slots_deleted(runner, [index])
            payload.update(
                {
                    "requested_indexes": [index],
                    "command_succeeded_indexes": [index],
                    **verification,
                }
            )
            if verification["verified"]:
                ops_state.clear_pending_inbound(index=index)
            operations_audit.record(
                operation="manual_single_delete",
                ok=bool(verification["verified"]),
                message=(
                    "manual SMS deletion verified"
                    if verification["verified"]
                    else "manual SMS deletion command completed but verification failed"
                ),
                requested_count=1,
                command_succeeded_count=1,
                verified=bool(verification["verified"]),
                verification_error=str(verification["verification_error"]),
                storage=verification["storage"],
            )
            if not verification["verified"]:
                payload["ok"] = False
                payload["message"] = "SMS deletion could not be verified"
            self.send_json(verification_status, payload)

        def handle_delete_sms_batch(self, data: dict[str, Any]) -> None:
            if data.get("confirm") != "DELETE_SMS_BATCH":
                self.send_json(
                    400,
                    {
                        "ok": False,
                        "message": "batch SMS delete requires confirmation token DELETE_SMS_BATCH",
                    },
                )
                return
            requested_indexes, validation_error = validate_batch_indexes(data.get("indexes"))
            if validation_error:
                self.send_json(400, {"ok": False, "message": validation_error})
                return

            inbox_result = runner.run_cardpulse(["--inbox"])
            if not inbox_result.ok:
                payload = result_payload(inbox_result)
                payload["message"] = "SMS inbox read failed; batch deletion was not attempted"
                self.send_json(500, payload)
                return

            available_indexes, inbox_validation_error = parse_verified_inbox_indexes(inbox_result.stdout)
            if inbox_validation_error:
                self.send_json(409, {"ok": False, "message": inbox_validation_error})
                return

            _, messages = parse_inbox_records(inbox_result.stdout)
            requested_set = set(requested_indexes)
            for message in messages:
                indexes = [str(index) for index in message.get("indexes", [])]
                if message.get("is_multipart") and requested_set.intersection(indexes):
                    if not message.get("multipart_complete"):
                        self.send_json(
                            409,
                            {
                                "ok": False,
                                "message": "incomplete multipart SMS cannot be batch deleted",
                            },
                        )
                        return
                    if not set(indexes).issubset(requested_set):
                        self.send_json(
                            409,
                            {
                                "ok": False,
                                "message": "multipart SMS must be selected as a complete group",
                            },
                        )
                        return

            missing_indexes = sorted(requested_set - available_indexes, key=int)
            if missing_indexes:
                self.send_json(
                    409,
                    {
                        "ok": False,
                        "message": "one or more selected SMS indexes are no longer present",
                        "missing_indexes": missing_indexes,
                    },
                )
                return

            deleted_indexes: list[str] = []
            for index in sorted(requested_indexes, key=int, reverse=True):
                delete_result = runner.run_cardpulse(["--delete-sms", index, "--confirm", "DELETE_SMS"])
                if not delete_result.ok:
                    payload = result_payload(delete_result)
                    payload.update(
                        {
                            "ok": False,
                            "message": f"batch SMS deletion stopped at index {index}",
                            "deleted_indexes": deleted_indexes,
                            "failed_index": index,
                            "verified": False,
                            "requested_indexes": requested_indexes,
                            "command_succeeded_indexes": deleted_indexes,
                        }
                    )
                    operations_audit.record(
                        operation="manual_batch_delete",
                        ok=False,
                        message="manual batch SMS deletion stopped after a delete failure",
                        requested_count=len(requested_indexes),
                        command_succeeded_count=len(deleted_indexes),
                    )
                    self.send_json(500, payload)
                    return
                deleted_indexes.append(index)

            verification_status, verification = verify_sms_slots_deleted(runner, requested_indexes)
            operations_audit.record(
                operation="manual_batch_delete",
                ok=bool(verification["verified"]),
                message=(
                    "manual batch SMS deletion verified"
                    if verification["verified"]
                    else "manual batch SMS deletion command completed but verification failed"
                ),
                requested_count=len(requested_indexes),
                command_succeeded_count=len(deleted_indexes),
                verified=bool(verification["verified"]),
                verification_error=str(verification["verification_error"]),
                storage=verification["storage"],
            )
            if verification["verified"]:
                for index in requested_indexes:
                    ops_state.clear_pending_inbound(index=index)
            self.send_json(
                verification_status,
                {
                    "ok": bool(verification["verified"]),
                    "message": (
                        f"deleted {len(deleted_indexes)} SMS storage slots"
                        if verification["verified"]
                        else "batch SMS deletion could not be verified"
                    ),
                    "deleted_indexes": deleted_indexes,
                    "failed_index": "",
                    "requested_indexes": requested_indexes,
                    "command_succeeded_indexes": deleted_indexes,
                    **verification,
                },
            )

        def handle_delete_incomplete_sms(self, data: dict[str, Any]) -> None:
            if data.get("confirm") != "FORCE_DELETE_INCOMPLETE_SMS":
                self.send_json(
                    400,
                    {
                        "ok": False,
                        "message": (
                            "incomplete multipart SMS delete requires confirmation token "
                            "FORCE_DELETE_INCOMPLETE_SMS"
                        ),
                    },
                )
                return
            requested_indexes, validation_error = validate_batch_indexes(data.get("indexes"))
            if validation_error:
                self.send_json(400, {"ok": False, "message": validation_error})
                return

            inbox_result = runner.run_cardpulse(["--inbox"])
            if not inbox_result.ok:
                payload = result_payload(inbox_result)
                payload["message"] = "SMS inbox read failed; incomplete multipart deletion was not attempted"
                self.send_json(500, payload)
                return

            _, inbox_validation_error = parse_verified_inbox_indexes(inbox_result.stdout)
            if inbox_validation_error:
                self.send_json(409, {"ok": False, "message": inbox_validation_error})
                return

            _, messages = parse_inbox_records(inbox_result.stdout)
            requested_set = set(requested_indexes)
            matching_messages = [
                message
                for message in messages
                if set(str(index) for index in message.get("indexes", [])) == requested_set
            ]
            if len(matching_messages) != 1:
                self.send_json(
                    409,
                    {
                        "ok": False,
                        "message": (
                            "selected SMS indexes do not match one current incomplete multipart SMS group"
                        ),
                    },
                )
                return

            message = matching_messages[0]
            indexes = [str(index) for index in message.get("indexes", [])]
            if (
                not indexes
                or len(indexes) != len(set(indexes))
                or not message.get("is_multipart")
                or message.get("multipart_complete")
            ):
                self.send_json(
                    409,
                    {
                        "ok": False,
                        "message": "force deletion is only available for an incomplete multipart SMS group",
                    },
                )
                return

            deleted_indexes: list[str] = []
            for index in sorted(indexes, key=int, reverse=True):
                delete_result = runner.run_cardpulse(["--delete-sms", index, "--confirm", "DELETE_SMS"])
                if not delete_result.ok:
                    payload = result_payload(delete_result)
                    payload.update(
                        {
                            "ok": False,
                            "message": f"incomplete multipart SMS deletion stopped at index {index}",
                            "deleted_indexes": deleted_indexes,
                            "failed_index": index,
                            "verified": False,
                            "requested_indexes": requested_indexes,
                            "command_succeeded_indexes": deleted_indexes,
                        }
                    )
                    operations_audit.record(
                        operation="manual_incomplete_multipart_delete",
                        ok=False,
                        message="manual incomplete multipart SMS deletion stopped after a delete failure",
                        requested_count=len(requested_indexes),
                        command_succeeded_count=len(deleted_indexes),
                    )
                    self.send_json(500, payload)
                    return
                deleted_indexes.append(index)

            verification_status, verification = verify_sms_slots_deleted(runner, indexes)
            operations_audit.record(
                operation="manual_incomplete_multipart_delete",
                ok=bool(verification["verified"]),
                message=(
                    "manual incomplete multipart SMS deletion verified"
                    if verification["verified"]
                    else "manual incomplete multipart SMS deletion command completed but verification failed"
                ),
                requested_count=len(indexes),
                command_succeeded_count=len(deleted_indexes),
                verified=bool(verification["verified"]),
                verification_error=str(verification["verification_error"]),
                storage=verification["storage"],
            )
            if verification["verified"]:
                for index in indexes:
                    ops_state.clear_pending_inbound(index=index)
            self.send_json(
                verification_status,
                {
                    "ok": bool(verification["verified"]),
                    "message": (
                        f"deleted {len(deleted_indexes)} incomplete multipart SMS storage slots"
                        if verification["verified"]
                        else "incomplete multipart SMS deletion could not be verified"
                    ),
                    "deleted_indexes": deleted_indexes,
                    "failed_index": "",
                    "requested_indexes": indexes,
                    "command_succeeded_indexes": deleted_indexes,
                    **verification,
                },
            )

        def handle_acknowledge_current_message(self, data: dict[str, Any]) -> None:
            message_id_value = str(data.get("id", "")).strip()
            indexes = data.get("indexes")
            if not message_id_value:
                self.send_json(400, {"ok": False, "message": "message id is required"})
                return
            if not isinstance(indexes, list) or not indexes or any(
                not is_sms_index(index) for index in indexes
            ):
                self.send_json(400, {"ok": False, "message": "SMS indexes are required"})
                return
            ops_state.clear_pending_inbound(message_id_value=message_id_value)
            self.send_json(
                200,
                {
                    "ok": True,
                    "message": "multipart SMS acknowledgement recorded locally",
                    "id": message_id_value,
                    "indexes": indexes,
                },
            )

        def handle_clear_local_history(self, data: dict[str, Any]) -> None:
            if data.get("confirm") != "CLEAR_LOCAL_HISTORY":
                self.send_json(
                    400,
                    {
                        "ok": False,
                        "message": "local history clear requires confirmation token CLEAR_LOCAL_HISTORY",
                    },
                )
                return
            cleared_count = history.clear()
            history_audit.record_clear(cleared_count=cleared_count, ok=True)
            self.send_json(
                200,
                {
                    "ok": True,
                    "cleared_count": cleared_count,
                    "message": "local message history cleared",
                },
            )

        def handle_current_messages(self) -> None:
            result = runner.run_cardpulse(["--inbox"])
            payload = result_payload(result)
            _, messages = parse_inbox_records(result.stdout)
            messages = [message for message in messages if message.get("indexes")]
            new_messages: list[dict[str, Any]] = []

            if result.ok:
                stored_messages = [history.add(message) for message in messages]
                new_messages = ops_state.note_current_messages(stored_messages)
                ops_state.clear_failure("inbox")
                if new_messages:
                    notify_new_messages(new_messages)
            else:
                ops_state.note_failure("当前模块短信列表读取失败", kind="inbox")

            payload.update(
                {
                    "source": "module",
                    "messages": messages,
                    "new_message_count": len(new_messages),
                    "new_messages": new_messages,
                }
            )
            self.send_json(200, payload)

        def handle_current_message(self, index: str) -> None:
            if not is_sms_index(index):
                self.send_json(400, {"ok": False, "message": "SMS index must be a single non-negative integer"})
                return

            result = runner.run_cardpulse(["--read-sms", index])
            payload = result_payload(result)
            blocks = parse_message_blocks(result.stdout)
            if result.ok and blocks:
                message = normalize_sms_message(blocks[0], index=index)
                stored = history.add(message)
                ops_state.note_message(stored)
                ops_state.clear_failure("read", "parse")
                payload["message"] = stored
            elif result.ok:
                payload["ok"] = False
                payload["message"] = "SMS read succeeded but no parseable message was returned"
                ops_state.note_failure(payload["message"], kind="parse")
            else:
                ops_state.note_failure("短信详情读取失败", kind="read")
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
                stored = history.add(outbound)
                ops_state.note_message(stored)
                ops_state.clear_failure("send")
                payload["message"] = stored
                self.send_json(200, payload)
            else:
                ops_state.note_failure("短信发送失败", kind="send")
                payload["message"] = outbound
                self.send_json(500, payload)

    return CardPulseWebHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local CardPulse Web control server.")
    parser.add_argument("--host", default=os.environ.get("CARDPULSE_WEB_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CARDPULSE_WEB_PORT", DEFAULT_PORT)))
    parser.add_argument("--socket", default=os.environ.get("CARDPULSE_WEB_SOCKET", ""))
    parser.add_argument("--base-path", default=os.environ.get("CARDPULSE_WEB_BASE_PATH", ""))
    parser.add_argument("--fnos-gateway", action="store_true", help="trust fnOS gateway administrator identity")
    parser.add_argument("--root", default=str(ROOT_DIR), help="CardPulse repository or install root")
    parser.add_argument("--allow-sms", action="store_true", help="allow the Web UI to run cardpulse --test")
    parser.add_argument(
        "--auth-file",
        default=os.environ.get("CARDPULSE_WEB_AUTH_FILE", ""),
        help="path to the CardPulse Web password hash file",
    )
    parser.add_argument(
        "--public-origin",
        default=os.environ.get("CARDPULSE_WEB_PUBLIC_ORIGIN", ""),
        help="exact HTTPS origin allowed to submit authenticated requests",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root_dir = Path(args.root).resolve()
    ui_path = root_dir / "web" / "index.html"
    allow_sms = args.allow_sms or os.environ.get("CARDPULSE_WEB_ALLOW_SMS") == "1"

    config_dir = Path(os.environ.get("CARDPULSE_CONFIG_DIR", str(root_dir / "config")))
    state_dir = Path(os.environ.get("CARDPULSE_STATE_DIR", str(root_dir / "state")))
    history_path = Path(os.environ.get("CARDPULSE_WEB_HISTORY_PATH", str(state_dir / "messages.jsonl")))
    ops_state_path = Path(os.environ.get("CARDPULSE_WEB_OPS_STATE_PATH", str(state_dir / "web-state.json")))
    recovery_state_path = Path(os.environ.get("CARDPULSE_WEB_RECOVERY_STATE_PATH", str(state_dir / "recovery.json")))
    operations_audit_path = Path(
        os.environ.get("CARDPULSE_WEB_OPERATIONS_AUDIT_PATH", str(state_dir / "sms-operations.jsonl"))
    )
    history_audit_path = Path(
        os.environ.get("CARDPULSE_WEB_HISTORY_AUDIT_PATH", str(state_dir / "local-history.jsonl"))
    )
    auth_path = Path(args.auth_file) if args.auth_file else config_dir / "web-auth.json"
    data_dir = Path(os.environ.get("CARDPULSE_DATA_DIR", str(state_dir.parent)))
    qdc507_default_path = (
        data_dir / "lifecycle" / "qdc507-readonly-acceptance.json"
        if args.fnos_gateway
        else state_dir / "qdc507-readonly-acceptance.json"
    )
    qdc507_acceptance_record_path = Path(
        os.environ.get(
            "CARDPULSE_QDC507_ACCEPTANCE_PATH",
            str(qdc507_default_path),
        )
    )
    qdc507_acceptance_path = qdc507_acceptance_record_path if args.fnos_gateway else None
    if args.fnos_gateway and not args.socket:
        parser.error("--fnos-gateway requires --socket")
    if args.fnos_gateway and args.base_path != FNOS_GATEWAY_BASE_PATH:
        parser.error(f"--fnos-gateway requires --base-path {FNOS_GATEWAY_BASE_PATH}")
    if args.fnos_gateway and args.public_origin:
        parser.error("--fnos-gateway does not accept --public-origin")

    strict_permissions = args.fnos_gateway
    ensure_private_directory(config_dir, strict=strict_permissions)
    ensure_private_directory(state_dir, strict=strict_permissions)
    private_files = [
        config_dir / "config.yaml",
        auth_path,
        history_path,
        ops_state_path,
        recovery_state_path,
        operations_audit_path,
        history_audit_path,
        state_dir / "last_success",
        state_dir / "last_success_date",
        state_dir / "history.log",
        state_dir / "history.lock",
        state_dir / "cardpulse.lock",
    ]
    if not args.fnos_gateway:
        private_files.append(qdc507_acceptance_record_path)
    for private_file in private_files:
        ensure_private_file(private_file, strict=strict_permissions)

    runner = CardPulseRunner(root_dir=root_dir)
    handler = make_handler(
        runner=runner,
        ui_path=ui_path,
        allow_sms=allow_sms,
        history_path=history_path,
        ops_state_path=ops_state_path,
        recovery_state_path=recovery_state_path,
        operations_audit_path=operations_audit_path,
        history_audit_path=history_audit_path,
        auth_path=auth_path,
        public_origin=args.public_origin,
        base_path=args.base_path,
        gateway_admin_only=args.fnos_gateway,
        scheduler_config_path=config_dir / "config.yaml",
        qdc507_acceptance_path=qdc507_acceptance_path,
    )
    if args.socket:
        socket_path = Path(args.socket)
        if args.fnos_gateway:
            ensure_gateway_socket_directory(socket_path.parent, strict=True)
        else:
            ensure_private_directory(socket_path.parent)
        if socket_path.exists():
            socket_path.unlink()
        server = ThreadingUnixHTTPServer(str(socket_path), handler)
        try:
            os.chmod(socket_path, 0o660)
        except OSError:
            if args.fnos_gateway:
                raise
        if args.fnos_gateway and os.name != "nt" and socket_path.stat().st_mode & 0o777 != 0o660:
            raise PermissionError(f"cannot secure {socket_path}")
        url = f"unix://{socket_path}"
    else:
        server = ThreadingHTTPServer((args.host, args.port), handler)
        url = f"http://{args.host}:{args.port}"
    print(f"CardPulse Web listening on {url}")
    if not allow_sms:
        print("SMS sending actions are disabled. Start with --allow-sms to enable guarded SMS endpoints.")
    record_startup_sms_storage_baseline(runner, handler.operations_audit)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if args.socket:
            try:
                Path(args.socket).unlink()
            except (FileNotFoundError, OSError):
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
