#!/usr/bin/env python3
"""Focused tests for lib/pdu_decoder.py."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DECODER = ROOT / "lib" / "pdu_decoder.py"

GSM_7BIT = (
    "@", "\u00a3", "$", "\u00a5", "\u00e8", "\u00e9", "\u00f9", "\u00ec",
    "\u00f2", "\u00c7", "\n", "\u00d8", "\u00f8", "\r", "\u00c5", "\u00e5",
    "\u0394", "_", "\u03a6", "\u0393", "\u039b", "\u03a9", "\u03a0", "\u03a8",
    "\u03a3", "\u0398", "\u039e", None, "\u00c6", "\u00e6", "\u00df", "\u00c9",
    " ", "!", '"', "#", "\u00a4", "%", "&", "'",
    "(", ")", "*", "+", ",", "-", ".", "/",
    "0", "1", "2", "3", "4", "5", "6", "7",
    "8", "9", ":", ";", "<", "=", ">", "?",
    "\u00a1", "A", "B", "C", "D", "E", "F", "G",
    "H", "I", "J", "K", "L", "M", "N", "O",
    "P", "Q", "R", "S", "T", "U", "V", "W",
    "X", "Y", "Z", "\u00c4", "\u00d6", "\u00d1", "\u00dc", "\u00a7",
    "\u00bf", "a", "b", "c", "d", "e", "f", "g",
    "h", "i", "j", "k", "l", "m", "n", "o",
    "p", "q", "r", "s", "t", "u", "v", "w",
    "x", "y", "z", "\u00e4", "\u00f6", "\u00f1", "\u00fc", "\u00e0",
)
GSM_EXT = {
    "^": 0x14,
    "{": 0x28,
    "}": 0x29,
    "\\": 0x2F,
    "[": 0x3C,
    "~": 0x3D,
    "]": 0x3E,
    "|": 0x40,
    "\u20ac": 0x65,
}
CHAR_TO_GSM = {}
for index, char in enumerate(GSM_7BIT):
    if char is not None:
        CHAR_TO_GSM[char] = (index, False)
for char, index in GSM_EXT.items():
    CHAR_TO_GSM[char] = (index, True)


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


def swap_digits(value: str) -> str:
    if len(value) % 2:
        value += "F"
    return "".join(value[i + 1] + value[i] for i in range(0, len(value), 2))


def gsm7_septets(text: str) -> list[int]:
    result: list[int] = []
    for char in text:
        index, is_extension = CHAR_TO_GSM[char]
        if is_extension:
            result.append(0x1B)
        result.append(index)
    return result


def pack_septets(septets: list[int], *, skip_bits: int = 0, prefix_bytes: bytes = b"") -> bytes:
    total_bits = skip_bits + len(septets) * 7
    out = bytearray(max(len(prefix_bytes), math.ceil(total_bits / 8)))
    for index, octet in enumerate(prefix_bytes):
        out[index] = octet
    bit_pos = skip_bits
    for septet in septets:
        for bit_index in range(7):
            if septet & (1 << bit_index):
                absolute = bit_pos + bit_index
                out[absolute // 8] |= 1 << (absolute % 8)
        bit_pos += 7
    return bytes(out)


def build_deliver_pdu(sender: str, dcs: int, udl: int, user_data: bytes, *, udhi: bool = False) -> str:
    digits = sender[1:] if sender.startswith("+") else sender
    toa = "91" if sender.startswith("+") else "81"
    first_octet = 0x04 | (0x40 if udhi else 0x00)
    return (
        "00"
        f"{first_octet:02X}"
        f"{len(digits):02X}"
        f"{toa}"
        f"{swap_digits(digits)}"
        "00"
        f"{dcs:02X}"
        "62708021436500"
        f"{udl:02X}"
        f"{user_data.hex().upper()}"
    )


def build_concat_deliver_pdu(sender: str, text: str, reference: int, total_parts: int, part_index: int) -> str:
    udh = bytes([0x05, 0x00, 0x03, reference, total_parts, part_index])
    septets = gsm7_septets(text)
    header_septets = math.ceil(len(udh) * 8 / 7)
    payload = pack_septets(septets, skip_bits=header_septets * 7, prefix_bytes=udh)
    udl = header_septets + len(septets)
    return build_deliver_pdu(sender, 0x00, udl, payload, udhi=True)


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

    ext_payload = pack_septets(gsm7_septets("Hello^"))
    ext = decoder.decode_pdu(build_deliver_pdu("+12345678901", 0x00, len(gsm7_septets("Hello^")), ext_payload))
    assert_ok(ext["text"] == "Hello^", ext)

    part1 = decoder.decode_pdu(build_concat_deliver_pdu("+12345678901", "Hello ", 7, 2, 1))
    part2 = decoder.decode_pdu(build_concat_deliver_pdu("+12345678901", "world", 7, 2, 2))
    assert_ok(part1["text"] == "Hello ", part1)
    assert_ok(part1["has_udh"] is True, part1)
    assert_ok(part1["concat_ref"] == "7", part1)
    assert_ok(part1["concat_total"] == 2, part1)
    assert_ok(part1["concat_seq"] == 1, part1)
    assert_ok(part2["text"] == "world", part2)
    assert_ok(part2["concat_total"] == 2, part2)
    assert_ok(part2["concat_seq"] == 2, part2)

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
