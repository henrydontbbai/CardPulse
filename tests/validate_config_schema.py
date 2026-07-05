#!/usr/bin/env python3
"""Validate config/config.example.yaml structure and basic value types."""

from pathlib import Path
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "config.example.yaml"


REQUIRED = {
    "serial": {
        "port": str,
        "baudrate": int,
        "auto_detect": bool,
    },
    "sms": {
        "phone": str,
        "message": str,
        "interval_days": int,
        "timeout": int,
    },
    "retry": {
        "max_attempts": int,
        "interval": int,
    },
    "logging": {
        "level": str,
        "file": str,
    },
}

NOTIFY_REQUIRED = {
    "enabled": bool,
    "telegram": {"enabled": bool, "bot_token": str, "chat_id": str},
    "wechat": {"enabled": bool, "send_key": str},
    "wecom": {"enabled": bool, "webhook_url": str},
    "qq": {"enabled": bool, "qmsg_key": str},
    "feishu": {"enabled": bool, "webhook_url": str},
    "dingtalk": {"enabled": bool, "webhook_url": str, "secret": str},
    "bark": {"enabled": bool, "url": str},
    "email": {
        "enabled": bool,
        "smtp_host": str,
        "smtp_port": int,
        "username": str,
        "password": str,
        "from": str,
        "to": str,
        "use_ssl": bool,
    },
}


def assert_type(path: str, value, expected: type) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{path} must be {expected.__name__}, got {type(value).__name__}")


def validate_section(config: dict, section: str, spec: dict) -> None:
    if section not in config:
        raise KeyError(f"missing section: {section}")
    data = config[section]
    assert_type(section, data, dict)
    for key, expected in spec.items():
        if key not in data:
            raise KeyError(f"missing key: {section}.{key}")
        assert_type(f"{section}.{key}", data[key], expected)


def validate_notify(config: dict) -> None:
    notify = config.get("notify")
    assert_type("notify", notify, dict)
    for key, expected in NOTIFY_REQUIRED.items():
        if key not in notify:
            raise KeyError(f"missing key: notify.{key}")
        value = notify[key]
        if isinstance(expected, dict):
            assert_type(f"notify.{key}", value, dict)
            for subkey, subtype in expected.items():
                if subkey not in value:
                    raise KeyError(f"missing key: notify.{key}.{subkey}")
                assert_type(f"notify.{key}.{subkey}", value[subkey], subtype)
        else:
            assert_type(f"notify.{key}", value, expected)


def main() -> int:
    with CONFIG.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    assert_type("config", config, dict)
    for section, spec in REQUIRED.items():
        validate_section(config, section, spec)
    validate_notify(config)

    baudrate = config["serial"]["baudrate"]
    if baudrate not in {9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600}:
        raise ValueError(f"invalid serial.baudrate: {baudrate}")
    if config["sms"]["interval_days"] < 1:
        raise ValueError("sms.interval_days must be positive")
    if config["retry"]["max_attempts"] < 1:
        raise ValueError("retry.max_attempts must be positive")

    phone = config["sms"]["phone"]
    stripped = "".join(ch for ch in phone if ch not in " .()-\t\n\r")
    if not stripped.startswith("+") and not stripped[0].isdigit():
        raise ValueError("sms.phone must start with + or a digit")
    if any(ch not in "+0123456789" for ch in stripped):
        raise ValueError("sms.phone contains unsupported characters")

    print("config schema ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"config schema validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
