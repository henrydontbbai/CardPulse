#!/usr/bin/env python3
"""Validate the public CardPulse HTTPS origin and derive a Caddy address."""

from __future__ import annotations

import argparse
import ipaddress
import re
from urllib.parse import urlsplit


HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.?$"
)


def normalize_origin(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid HTTPS public origin") from exc

    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid HTTPS public origin")

    try:
        host_ip = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            hostname = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("invalid HTTPS public origin") from exc
        if not HOSTNAME_RE.fullmatch(hostname):
            raise ValueError("invalid HTTPS public origin")
        hostname = hostname.rstrip(".")
        authority = hostname
    else:
        authority = f"[{host_ip.compressed}]" if host_ip.version == 6 else str(host_ip)

    if port is not None and port != 443:
        authority = f"{authority}:{port}"
    return f"https://{authority}", authority


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("origin")
    args = parser.parse_args()
    try:
        normalized_origin, caddy_address = normalize_origin(args.origin)
    except ValueError as exc:
        parser.error(str(exc))
    print(normalized_origin)
    print(caddy_address)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
