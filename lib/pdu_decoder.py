#!/usr/bin/env python3
"""Decode common SMS-DELIVER PDU payloads for CardPulse inbox views."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass


GSM7 = (
    "@\u00a3$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ"
    "\x1bÆæßÉ !\"#¤%&'()*+,-./"
    "0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)


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
    digits = []
    for i in range(0, len(value), 2):
        pair = value[i : i + 2]
        if len(pair) == 2:
            digits.extend([pair[1], pair[0]])
    decoded = "".join(d for d in digits if d.upper() != "F")
    if length is not None:
        decoded = decoded[:length]
    return decoded


def decode_timestamp(value: str) -> str:
    digits = [decode_swapped_digits(value[i : i + 2]) for i in range(0, 12, 2)]
    if len(digits) < 6:
        return ""
    year = int(digits[0])
    century = 2000 if year < 70 else 1900
    return f"{century + year:04d}-{digits[1]}-{digits[2]} {digits[3]}:{digits[4]}:{digits[5]}"


def decode_gsm7(data_hex: str, septet_count: int) -> str:
    data = bytes.fromhex(data_hex)
    chars: list[str] = []
    escape = False
    for i in range(septet_count):
        bit_pos = i * 7
        byte_pos = bit_pos // 8
        shift = bit_pos % 8
        if byte_pos >= len(data):
            break
        value = (data[byte_pos] >> shift) & 0x7F
        if shift > 1 and byte_pos + 1 < len(data):
            value |= (data[byte_pos + 1] << (8 - shift)) & 0x7F
        if escape:
            chars.append(" ")
            escape = False
        elif value == 0x1B:
            escape = True
        else:
            chars.append(GSM7[value] if value < len(GSM7) else "?")
    return "".join(chars)


def decode_user_data(dcs: int, ud_hex: str, udl: int, has_udh: bool) -> str:
    if has_udh and ud_hex:
        udh_len = int(ud_hex[:2], 16)
        ud_hex = ud_hex[(udh_len + 1) * 2 :]
        if dcs == 0x08:
            udl = max(0, udl - udh_len - 1)

    if dcs == 0x08:
        return bytes.fromhex(ud_hex[: udl * 2]).decode("utf-16-be", errors="replace")
    if dcs == 0x00 or (dcs & 0x0C) == 0x00:
        return decode_gsm7(ud_hex, udl)
    return bytes.fromhex(ud_hex[: udl * 2]).decode("latin-1", errors="replace")


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

    cur.take_octet()  # PID
    dcs = cur.take_octet()
    timestamp = decode_timestamp(cur.take(7))
    udl = cur.take_octet()
    ud_hex = pdu[cur.pos :]
    text = decode_user_data(dcs, ud_hex, udl, bool(first_octet & 0x40))

    return {
        "ok": True,
        "sender": sender,
        "timestamp": timestamp,
        "text": text,
        "dcs": f"{dcs:02X}",
        "raw_pdu": pdu,
    }


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
