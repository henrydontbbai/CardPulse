#!/usr/bin/env python3
"""
CardPulse - PDU 编码器模块
实现 3GPP TS 23.040 / GSM 03.38 规范的 PDU 编码。
仅使用 Python3 标准库，无外部依赖。
"""

import sys
import re

# GSM 03.38 7-bit default alphabet (indices 0-127)
GSM_7BIT = (
    '@', '\u00a3', '$', '\u00a5', '\u00e8', '\u00e9', '\u00f9', '\u00ec',
    '\u00f2', '\u00c7', '\n', '\u00d8', '\u00f8', '\r', '\u00c5', '\u00e5',
    '\u0394', '_', '\u03a6', '\u0393', '\u039b', '\u03a9', '\u03a0', '\u03a8',
    '\u03a3', '\u0398', '\u039e', '\uffff', '\u00c6', '\u00e6', '\u00df', '\u00c9',
    ' ', '!', '"', '#', '\u00a4', '%', '&', "'",
    '(', ')', '*', '+', ',', '-', '.', '/',
    '0', '1', '2', '3', '4', '5', '6', '7',
    '8', '9', ':', ';', '<', '=', '>', '?',
    '\u00a1', 'A', 'B', 'C', 'D', 'E', 'F', 'G',
    'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O',
    'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W',
    'X', 'Y', 'Z', '\u00c4', '\u00d6', '\u00d1', '\u00dc', '\u00a7',
    '\u00bf', 'a', 'b', 'c', 'd', 'e', 'f', 'g',
    'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o',
    'p', 'q', 'r', 's', 't', 'u', 'v', 'w',
    'x', 'y', 'z', '\u00e4', '\u00f6', '\u00f1', '\u00fc', '\u00e0',
)

# GSM 03.38 extension table values (after escape 0x1B).
GSM_EXT = {
    '^': 0x14,
    '{': 0x28,
    '}': 0x29,
    '\\': 0x2F,
    '[': 0x3C,
    '~': 0x3D,
    ']': 0x3E,
    '|': 0x40,
    '\u20ac': 0x65,
}

# Build reverse lookup: char -> (index_in_main, is_extension)
CHAR_TO_GSM = {}
for i, ch in enumerate(GSM_7BIT):
    if ch != '\uffff':
        CHAR_TO_GSM[ch] = (i, False)
for ch, idx in GSM_EXT.items():
    CHAR_TO_GSM[ch] = (idx, True)


def gsm7_septets(text):
    """Encode text to GSM 03.38 septets."""
    septets = []
    for ch in text:
        if ch not in CHAR_TO_GSM:
            raise ValueError(f'字符不能使用 GSM 7-bit 编码: {ch!r}')
        idx, is_ext = CHAR_TO_GSM[ch]
        if is_ext:
            septets.append(0x1B)
        septets.append(idx)
    return septets


def encode_gsm7bit(text):
    """Encode text to packed GSM 7-bit user-data hex and septet count."""
    septets = gsm7_septets(text)
    output = []
    bit_buffer = 0
    bit_count = 0

    for septet in septets:
        bit_buffer |= (septet & 0x7F) << bit_count
        bit_count += 7
        while bit_count >= 8:
            output.append(bit_buffer & 0xFF)
            bit_buffer >>= 8
            bit_count -= 8

    if bit_count:
        output.append(bit_buffer & 0xFF)

    return ''.join(f'{octet:02X}' for octet in output), len(septets)


def encode_ucs2(text):
    """Encode text to UCS2 hex string."""
    result = ''
    for ch in text:
        cp = ord(ch)
        if cp > 0xFFFF:
            # Supplementary planes - encode as surrogate pair
            cp -= 0x10000
            high = 0xD800 | ((cp >> 10) & 0x3FF)
            low = 0xDC00 | (cp & 0x3FF)
            result += f'{high:04X}{low:04X}'
        else:
            result += f'{cp:04X}'
    return result


def needs_ucs2(text):
    """Check if text contains characters requiring UCS2 encoding."""
    for ch in text:
        if ch not in CHAR_TO_GSM:
            return True
    return False


def validate_phone(phone):
    """Validate and normalize phone number. Returns (normalized, is_international)."""
    phone = re.sub(r'[\s().-]', '', phone.strip())
    if not phone:
        return None, False
    if not re.fullmatch(r'\+?[0-9]{5,15}', phone):
        return None, phone.startswith('+')
    is_international = phone.startswith('+')
    return phone, is_international


def get_toa(is_international):
    """Get Type-of-Address octet."""
    if is_international:
        return '91'  # International, ISDN
    return '81'  # Unknown, ISDN


def semi_octet_encode(number):
    """Encode phone number in semi-octet format."""
    # Remove leading +
    if number.startswith('+'):
        number = number[1:]
    # Pad with F if odd length
    if len(number) % 2 == 1:
        number += 'F'
    # Swap pairs
    result = ''
    for i in range(0, len(number), 2):
        result += number[i+1] + number[i]
    return result


def build_pdu(phone, message):
    """
    Build SMS-SUBMIT PDU.
    Returns (pdu_hex, pdu_octets) on success, or (None, error_msg) on failure.
    """
    phone, is_international = validate_phone(phone)
    if phone is None:
        return None, '无效的电话号码格式'

    # Strip control characters from message
    message = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1A\x1C-\x1F]', '', message)
    if not message:
        return None, '短信内容为空'

    use_ucs2 = needs_ucs2(message)

    if use_ucs2:
        dcs = '08'  # UCS2
        encoded = encode_ucs2(message)
        udl_octets = len(encoded) // 2
        if udl_octets > 140:
            return None, f'短信内容过长: UCS2 {udl_octets} octets，单条短信最多 140 octets'
        udl = format(udl_octets, '02X')
    else:
        dcs = '00'  # GSM 7-bit default alphabet
        encoded, septet_count = encode_gsm7bit(message)
        if septet_count > 160:
            return None, f'短信内容过长: GSM 7-bit {septet_count} septets，单条短信最多 160 septets'
        udl = format(septet_count, '02X')

    # SMS-SUBMIT fields (3GPP TS 23.040)
    # MTI byte: bits 0-1 = 01 (SMS-SUBMIT), bit 2 = 0 (no validity period format in this byte)
    # VPF in bits 3-4 = 10 (relative format), bit 5 = 0 (no UDH), bit 6 = 0 (no reply path)
    # RD bit 7 = 0
    mti = '11'  # VPF=10 (relative), RD=0, SMS-SUBMIT
    mr = '00'   # Message Reference

    # Destination Address
    da_raw = phone[1:] if phone.startswith('+') else phone
    da_hex = semi_octet_encode(da_raw)
    da_len = format(len(da_raw), '02X')
    da_type = get_toa(is_international)

    # Protocol Identifier
    pid = '00'

    # Validity Period (relative, 24 hours)
    vp = 'A7'

    # User Data Length
    udl_hex = format(int(udl, 16), '02X')

    # Build PDU: SMSC + TPDU
    smsc = '00'  # Use default SMSC
    tpdu = mti + mr + da_len + da_type + da_hex + pid + dcs + vp + udl_hex + encoded

    return smsc + tpdu, None


def main():
    if len(sys.argv) < 3:
        print('Usage: pdu_encoder.py <phone> <message>', file=sys.stderr)
        sys.exit(1)

    phone = sys.argv[1]
    message = sys.argv[2]

    pdu, error = build_pdu(phone, message)
    if error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)

    # Output: pdu_hex tpdu_octet_count
    tpdu_len = len(pdu) // 2 - 1  # Exclude SMSC length octet
    print(f'{pdu} {tpdu_len}')


if __name__ == '__main__':
    main()
