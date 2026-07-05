#!/usr/bin/env python3
"""Focused tests for lib/pdu_encoder.py."""

from pathlib import Path
import importlib.util
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
ENCODER = ROOT / "lib" / "pdu_encoder.py"


def load_encoder():
    spec = importlib.util.spec_from_file_location("pdu_encoder", ENCODER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def assert_ok(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def expect_cli_failure(phone: str, message: str) -> None:
    proc = subprocess.run(
        [sys.executable, str(ENCODER), phone, message],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert_ok(proc.returncode != 0, f"expected failure for {phone!r} {message!r}")


def main() -> int:
    encoder = load_encoder()

    pdu, err = encoder.build_pdu("+8613800138000", "hello")
    assert_ok(err is None, str(err))
    assert_ok(pdu.endswith("05E8329BFD06"), pdu)

    pdu, err = encoder.build_pdu("+8613800138000", "你好")
    assert_ok(err is None, str(err))
    assert_ok(pdu.endswith("08A7044F60597D"), pdu)

    pdu, err = encoder.build_pdu("+8613800138000", "{}[]~|€")
    assert_ok(err is None, str(err))
    assert_ok("00A70E" in pdu, pdu)

    expect_cli_failure("1234", "test")
    expect_cli_failure("+8613800138000", "")
    expect_cli_failure("+8613800138000", "a" * 161)
    expect_cli_failure("+8613800138000", "你" * 71)

    print("pdu tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
