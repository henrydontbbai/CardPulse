"""Small local authentication primitives for CardPulse Web."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


AUTH_VERSION = 1
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32
LOGIN_CSRF_TTL = timedelta(minutes=10)
SESSION_IDLE_TTL = timedelta(minutes=30)
SESSION_ABSOLUTE_TTL = timedelta(hours=12)
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW = timedelta(minutes=15)
LOGIN_LOCK_TTL = timedelta(minutes=15)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)


def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
    )


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _ensure_private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _atomic_write_private_text(path: Path, content: str) -> None:
    path = Path(path)
    _ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
        _ensure_private_file(path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path.exists():
            temporary_path.unlink()


def write_password_config(path: Path, password: str) -> None:
    if not password:
        raise ValueError("password must not be empty")
    path = Path(path)
    _ensure_private_directory(path.parent)
    salt = secrets.token_bytes(SALT_BYTES)
    payload = {
        "version": AUTH_VERSION,
        "kdf": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": _encode(salt),
        "key": _encode(_derive_key(password, salt)),
    }
    _atomic_write_private_text(path, json.dumps(payload, sort_keys=True) + "\n")


def _load_password_config(path: Path) -> tuple[bytes, bytes] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != AUTH_VERSION:
            return None
        if payload.get("kdf") != "scrypt":
            return None
        salt = _decode(str(payload["salt"]))
        expected = _decode(str(payload["key"]))
    except (KeyError, ValueError, OSError, UnicodeEncodeError, binascii.Error, json.JSONDecodeError):
        return None
    if len(salt) != SALT_BYTES or len(expected) != KEY_BYTES:
        return None
    return salt, expected


def password_config_is_valid(path: Path) -> bool:
    return _load_password_config(path) is not None


def verify_password(path: Path, password: str) -> bool:
    config = _load_password_config(path)
    if not config:
        return False
    salt, expected = config
    derived = _derive_key(password, salt)
    return secrets.compare_digest(derived, expected)


@dataclass(frozen=True)
class Session:
    token: str
    csrf_token: str
    source: str
    created_at: datetime
    last_seen_at: datetime


class SessionStore:
    def __init__(self, now: Callable[[], datetime] = utc_now) -> None:
        self._now = now
        self._sessions: dict[str, Session] = {}
        self._login_csrf: dict[str, tuple[str, datetime]] = {}
        self._failures: dict[str, list[datetime]] = {}
        self._locks: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def issue_login_csrf(self, source: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._login_csrf[token] = (source, self._now() + LOGIN_CSRF_TTL)
        return token

    def consume_login_csrf(self, source: str, token: str) -> bool:
        with self._lock:
            record = self._login_csrf.get(token)
            if not record:
                return False
            token_source, expires_at = record
            if token_source != source or self._now() >= expires_at:
                return False
            self._login_csrf.pop(token, None)
            return True

    def is_login_locked(self, source: str) -> bool:
        now = self._now()
        with self._lock:
            locked_until = self._locks.get(source)
            if locked_until and now < locked_until:
                return True
            self._locks.pop(source, None)
            return False

    def record_login_failure(self, source: str) -> bool:
        now = self._now()
        with self._lock:
            recent = [
                at
                for at in self._failures.get(source, [])
                if now - at < LOGIN_FAILURE_WINDOW
            ]
            recent.append(now)
            self._failures[source] = recent
            if len(recent) >= LOGIN_FAILURE_LIMIT:
                self._locks[source] = now + LOGIN_LOCK_TTL
                self._failures.pop(source, None)
                return True
        return False

    def clear_login_failures(self, source: str) -> None:
        with self._lock:
            self._failures.pop(source, None)
            self._locks.pop(source, None)

    def create_session(self, source: str) -> Session:
        now = self._now()
        session = Session(
            token=secrets.token_urlsafe(48),
            csrf_token=secrets.token_urlsafe(32),
            source=source,
            created_at=now,
            last_seen_at=now,
        )
        with self._lock:
            self._sessions[session.token] = session
        return session

    def get_session(self, token: str) -> Session | None:
        now = self._now()
        with self._lock:
            session = self._sessions.get(token)
            if not session:
                return None
            if (
                now - session.last_seen_at >= SESSION_IDLE_TTL
                or now - session.created_at >= SESSION_ABSOLUTE_TTL
            ):
                self._sessions.pop(token, None)
                return None
            refreshed = Session(
                token=session.token,
                csrf_token=session.csrf_token,
                source=session.source,
                created_at=session.created_at,
                last_seen_at=now,
            )
            self._sessions[token] = refreshed
            return refreshed

    def delete_session(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)
