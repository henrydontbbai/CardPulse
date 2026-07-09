#!/usr/bin/env python3
"""Decode common SMS-DELIVER PDU payloads for CardPulse inbox views."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass


GSM7 = (
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
    0x0A: "\f",
    0x14: "^",
    0x28: "{",
    0x29: "}",
    0x2F: "\\",
    0x3C: "[",
    0x3D: "~",
    0x3E: "]",
    0x40: "|",
    0x65: "\u20ac",
}


@dataclass
class Cursor:
    pdu: str
    pos: int = 0

    def take(self, octets: int) -> str:
        size = octets * 2
        value = self.pdu[self.pos : self.pos + size]
        if len(value) != size:
            raise ValueError("PDU ended unexpectedly")
        self.pos += size
        return value

    def take_octet(self) -> int:
        return int(self.take(1), 16)


def decode_swapped_digits(value: str, length: int | None = None) -> str:
    digits: list[str] = []
    for index in range(0, len(value), 2):
        pair = value[index : index + 2]
        if len(pair) == 2:
            digits.extend([pair[1], pair[0]])
    decoded = "".join(digit for digit in digits if digit.upper() != "F")
    if length is not None:
        decoded = decoded[:length]
    return decoded


def decode_timestamp(value: str) -> str:
    digits = [decode_swapped_digits(value[index : index + 2]) for index in range(0, 12, 2)]
    if len(digits) < 6:
        return ""
    year = int(digits[0])
    century = 2000 if year < 70 else 1900
    return f"{century + year:04d}-{digits[1]}-{digits[2]} {digits[3]}:{digits[4]}:{digits[5]}"


def decode_gsm7(data_hex: str, septet_count: int, *, skip_bits: int = 0) -> str:
    data = bytes.fromhex(data_hex)
    chars: list[str] = []
    escape = False
    for index in range(septet_count):
        bit_pos = skip_bits + index * 7
        byte_pos = bit_pos // 8
        shift = bit_pos % 8
        if byte_pos >= len(data):
            break
        value = (data[byte_pos] >> shift) & 0x7F
        bits_from_next = shift + 7 - 8
        if bits_from_next > 0 and byte_pos + 1 < len(data):
            value |= (data[byte_pos + 1] & ((1 << bits_from_next) - 1)) << (8 - shift)
        value &= 0x7F
        if escape:
            chars.append(GSM_EXT.get(value, " "))
            escape = False
        elif value == 0x1B:
            escape = True
        else:
            chars.append(GSM7[value] if value < len(GSM7) and GSM7[value] is not None else "?")
    return "".join(chars)


def parse_udh(udh_hex: str) -> dict[str, object]:
    if not udh_hex:
        return {}

    udh = bytes.fromhex(udh_hex)
    index = 0
    payload: dict[str, object] = {}
    while index + 1 < len(udh):
        iei = udh[index]
        ie_len = udh[index + 1]
        start = index + 2
        end = start + ie_len
        if end > len(udh):
            break
        ie_data = udh[start:end]
        if iei == 0x00 and ie_len == 3:
            payload["concat_ref"] = str(ie_data[0])
            payload["concat_total"] = ie_data[1]
            payload["concat_seq"] = ie_data[2]
        elif iei == 0x08 and ie_len == 4:
            payload["concat_ref"] = str((ie_data[0] << 8) | ie_data[1])
            payload["concat_total"] = ie_data[2]
            payload["concat_seq"] = ie_data[3]
        index = end
    return payload


def decode_user_data(dcs: int, ud_hex: str, udl: int, has_udh: bool) -> tuple[str, dict[str, object]]:
    metadata: dict[str, object] = {"has_udh": has_udh}
    if has_udh and ud_hex:
        udh_len = int(ud_hex[:2], 16)
        header_octets = udh_len + 1
        header_hex_len = header_octets * 2
        udh_hex = ud_hex[2:header_hex_len]
        metadata.update(parse_udh(udh_hex))
        if dcs == 0x08:
            text_hex = ud_hex[header_hex_len:]
            udl = max(0, udl - header_octets)
            return (
                bytes.fromhex(text_hex[: udl * 2]).decode("utf-16-be", errors="replace"),
                metadata,
            )

        header_septets = math.ceil(header_octets * 8 / 7)
        text = decode_gsm7(ud_hex, max(0, udl - header_septets), skip_bits=header_septets * 7)
        return text, metadata

    if dcs == 0x08:
        return bytes.fromhex(ud_hex[: udl * 2]).decode("utf-16-be", errors="replace"), metadata
    if dcs == 0x00 or (dcs & 0x0C) == 0x00:
        return decode_gsm7(ud_hex, udl), metadata
    return bytes.fromhex(ud_hex[: udl * 2]).decode("latin-1", errors="replace"), metadata


def decode_pdu(raw_pdu: str) -> dict[str, object]:
    pdu = "".join(raw_pdu.split()).upper()
    if not pdu or any(ch not in "0123456789ABCDEF" for ch in pdu) or len(pdu) % 2:
        raise ValueError("invalid PDU hex")

    cur = Cursor(pdu)
    smsc_len = cur.take_octet()
    cur.take(smsc_len)

    first_octet = cur.take_octet()
    pdu_type = first_octet & 0x03
    if pdu_type != 0:
        raise ValueError("only SMS-DELIVER PDU is supported")

    oa_len = cur.take_octet()
    toa = cur.take_octet()
    oa_hex = cur.take((oa_len + 1) // 2)
    sender = decode_swapped_digits(oa_hex, oa_len)
    if toa & 0x90 == 0x90:
        sender = "+" + sender

    cur.take_octet()
    dcs = cur.take_octet()
    timestamp = decode_timestamp(cur.take(7))
    udl = cur.take_octet()
    ud_hex = pdu[cur.pos :]
    text, metadata = decode_user_data(dcs, ud_hex, udl, bool(first_octet & 0x40))

    payload: dict[str, object] = {
        "ok": True,
        "sender": sender,
        "timestamp": timestamp,
        "text": text,
        "dcs": f"{dcs:02X}",
        "raw_pdu": pdu,
    }
    payload.update(metadata)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Decode one SMS PDU")
    parser.add_argument("pdu")
    args = parser.parse_args()
    try:
        payload = decode_pdu(args.pdu)
    except Exception as exc:  # noqa: BLE001 - CLI should return a structured decode failure
        payload = {"ok": False, "error": str(exc), "raw_pdu": args.pdu}
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
