#!/usr/bin/env python3
"""Explicit, local-only migration from an exported WSL CardPulse state directory."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


SUPPORTED_CONFIG: dict[str, Any] = {
    "serial": {"port": None, "baudrate": None, "auto_detect": None},
    "sms": {"phone": None, "message": None, "interval_days": None, "timeout": None},
    "retry": {"max_attempts": None, "interval": None},
    "notify": {
        "enabled": None,
        "telegram": {"enabled": None, "bot_token": None, "chat_id": None},
        "wechat": {"enabled": None, "send_key": None},
        "wecom": {"enabled": None, "webhook_url": None},
        "qq": {"enabled": None, "qmsg_key": None},
        "feishu": {"enabled": None, "webhook_url": None},
        "dingtalk": {"enabled": None, "webhook_url": None, "secret": None},
        "bark": {"enabled": None, "url": None},
        "email": {
            "enabled": None,
            "smtp_host": None,
            "smtp_port": None,
            "username": None,
            "password": None,
            "from": None,
            "to": None,
            "use_ssl": None,
        },
    },
}
HISTORY_RETENTION_DAYS = 90
NAS_STATE_ROOT = Path("/var/lib/cardpulse")


def parse_first_seen_at(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def read_yaml_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def supported_config_text(text: str) -> str:
    if not text.strip():
        return ""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError("source config.yaml is invalid") from exc
    if not isinstance(parsed, dict):
        raise ValueError("source config.yaml must be a mapping")

    def select(values: dict[str, Any], allowed: dict[str, Any]) -> dict[str, Any]:
        selected: dict[str, Any] = {}
        for key, child in allowed.items():
            value = values.get(key)
            if child is None:
                if value is not None:
                    selected[key] = value
            elif isinstance(value, dict):
                nested = select(value, child)
                if nested:
                    selected[key] = nested
        return selected

    selected = select(parsed, SUPPORTED_CONFIG)
    return yaml.safe_dump(selected, allow_unicode=True, sort_keys=False) if selected else ""


def rewrite_serial_config(text: str) -> str:
    lines = text.splitlines()
    rewritten: list[str] = []
    in_serial = False
    saw_port = False
    saw_auto_detect = False
    for line in lines:
        stripped = line.strip()
        if stripped == "serial:":
            in_serial = True
            rewritten.append(line)
            continue
        if in_serial and stripped and not line.startswith((" ", "\t")):
            if not saw_port:
                rewritten.append("  port: /dev/cardpulse-at")
            if not saw_auto_detect:
                rewritten.append("  auto_detect: false")
            in_serial = False
        if in_serial and stripped.startswith("port:"):
            rewritten.append("  port: /dev/cardpulse-at")
            saw_port = True
            continue
        if in_serial and stripped.startswith("auto_detect:"):
            rewritten.append("  auto_detect: false")
            saw_auto_detect = True
            continue
        rewritten.append(line)
    if not any(line.strip() == "serial:" for line in lines):
        rewritten = ["serial:", "  port: /dev/cardpulse-at", "  auto_detect: false", *rewritten]
    elif in_serial:
        if not saw_port:
            rewritten.append("  port: /dev/cardpulse-at")
        if not saw_auto_detect:
            rewritten.append("  auto_detect: false")
    return "\n".join(rewritten).rstrip() + "\n"


def valid_history_records(
    path: Path,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.is_file():
        return records
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        first_seen_at = parse_first_seen_at(record.get("first_seen_at")) if isinstance(record, dict) else None
        age = current_time - first_seen_at if first_seen_at else None
        if (
            isinstance(record, dict)
            and record.get("id")
            and age is not None
            and timedelta(0) <= age < timedelta(days=HISTORY_RETENTION_DAYS)
        ):
            records.append(record)
    return records


def copy_file(
    source: Path,
    target: Path,
    owner: tuple[int, int] | None,
    dry_run: bool,
) -> None:
    if not source.is_file():
        return
    print(f"copy {source.name}")
    if not dry_run:
        atomic_write_bytes(target, source.read_bytes(), owner)


def set_private_mode(path: Path, mode: int) -> None:
    os.chmod(path, mode)


def resolve_cardpulse_owner() -> tuple[int, int]:
    if os.name == "nt":
        raise RuntimeError("cardpulse ownership requires a POSIX NAS")
    import grp
    import pwd

    try:
        account = pwd.getpwnam("cardpulse")
        group = grp.getgrnam("cardpulse")
    except KeyError as exc:
        raise RuntimeError("cardpulse service account is not installed") from exc
    return account.pw_uid, group.gr_gid


def set_cardpulse_owner(path: Path, owner: tuple[int, int]) -> None:
    os.chown(path, owner[0], owner[1])


def atomic_write_bytes(
    path: Path,
    content: bytes,
    owner: tuple[int, int] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temporary_name)
    owner_set_on_descriptor = False
    try:
        try:
            os.fchmod(file_descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        if owner is not None:
            try:
                os.fchown(file_descriptor, owner[0], owner[1])
                owner_set_on_descriptor = True
            except (AttributeError, OSError):
                pass
        with os.fdopen(file_descriptor, "wb") as handle:
            file_descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        set_private_mode(path, 0o600)
        if owner is not None and not owner_set_on_descriptor:
            set_cardpulse_owner(path, owner)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        temp_path.unlink(missing_ok=True)


def atomic_write(
    path: Path,
    content: str,
    owner: tuple[int, int] | None = None,
) -> None:
    atomic_write_bytes(path, content.encode("utf-8"), owner)


def regular_file_or_missing(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise ValueError(f"refusing non-regular migration path: {path}")
    return True


def validate_apply_target(path: Path) -> Path:
    target = path.absolute()
    expected = NAS_STATE_ROOT.absolute()
    if target.is_symlink() or target.resolve() != expected.resolve():
        raise ValueError(f"--target must be {expected}")
    for directory in (target.parent, target):
        if not directory.exists():
            continue
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"unsafe migration directory: {directory}")
        if os.name != "nt" and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError(f"migration directory is group/world writable: {directory}")
    return target.resolve()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    source = args.source.resolve()
    target = args.target.resolve()
    dry_run = args.dry_run
    if not source.is_dir():
        parser.error("--source must be an exported WSL state directory")
    if args.apply and os.name != "nt" and os.geteuid() != 0:
        parser.error("--apply must run as root")
    if args.apply:
        try:
            target = validate_apply_target(args.target)
        except ValueError as exc:
            parser.error(str(exc))

    source_config = source / "config.yaml"
    if not source_config.is_file() and (source / "config" / "config.yaml").is_file():
        source_config = source / "config" / "config.yaml"
    source_state = source / "state"
    if not source_state.is_dir():
        source_state = source
    config_text = rewrite_serial_config(supported_config_text(read_yaml_text(source_config)))
    history_records = valid_history_records(source_state / "messages.jsonl")

    print(f"source: {source}")
    print(f"target: {target}")
    print(f"history records with trusted first_seen_at: {len(history_records)}")
    print("timer: unchanged (not enabled)")
    if dry_run:
        return 0

    owner = resolve_cardpulse_owner()
    backup_root = target / "backup"
    backup_dir = backup_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    config_dir = target / "config"
    state_dir = target / "state"
    backup_root.mkdir(parents=True, exist_ok=True)
    set_private_mode(backup_root, 0o700)
    suffix = 1
    while backup_dir.exists():
        backup_dir = backup_root / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{suffix}"
        )
        suffix += 1
    backup_dir.mkdir()
    for path in (target, backup_root, config_dir, state_dir, backup_dir):
        path.mkdir(parents=True, exist_ok=True)
        set_private_mode(path, 0o700)
        set_cardpulse_owner(path, owner)

    for existing in (
        config_dir / "config.yaml",
        state_dir / "last_success",
        state_dir / "last_success_date",
        state_dir / "history.log",
        state_dir / "messages.jsonl",
    ):
        if regular_file_or_missing(existing):
            copy_file(existing, backup_dir / existing.name, owner, dry_run=False)

    if config_text:
        atomic_write(config_dir / "config.yaml", config_text, owner)
    for name in ("last_success", "last_success_date", "history.log"):
        copy_file(source_state / name, state_dir / name, owner, dry_run=False)
    messages_path = state_dir / "messages.jsonl"
    atomic_write(
        messages_path,
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in history_records),
        owner,
    )
    print(f"backup: {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
