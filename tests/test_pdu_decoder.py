#!/usr/bin/env python3
"""Focused tests for lib/pdu_decoder.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DECODER = ROOT / "lib" / "pdu_decoder.py"


def load_decoder():
    spec = importlib.util.spec_from_file_location("pdu_decoder", DECODER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def assert_ok(condition, message):
    if not condition:
        raise AssertionError(message)


def main() -> int:
    decoder = load_decoder()

    gsm7 = decoder.decode_pdu("00040B912143658709F100006270802143650005E8329BFD06")
    assert_ok(gsm7["sender"] == "+12345678901", gsm7)
    assert_ok(gsm7["timestamp"] == "2026-07-08 12:34:56", gsm7)
    assert_ok(gsm7["text"] == "hello", gsm7)
    assert_ok(gsm7["dcs"] == "00", gsm7)

    ucs2 = decoder.decode_pdu("00040D91683120553293F100086270807164000004004F004B")
    assert_ok(ucs2["sender"] == "+8613025523391", ucs2)
    assert_ok(ucs2["text"] == "OK", ucs2)
    assert_ok(ucs2["dcs"] == "08", ucs2)

    try:
        decoder.decode_pdu("not-hex")
    except ValueError as exc:
        assert_ok("invalid PDU hex" in str(exc), exc)
    else:
        raise AssertionError("invalid PDU should fail")

    try:
        decoder.decode_pdu("0011000B912143658709F10000AA05E8329BFD06")
    except ValueError as exc:
        assert_ok("SMS-DELIVER" in str(exc), exc)
    else:
        raise AssertionError("non-deliver PDU should fail")

    print("pdu decoder tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
