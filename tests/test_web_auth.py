#!/usr/bin/env python3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
import os
import sys
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT_DIR / "lib"))

import web_auth  # noqa: E402


class MutableClock:
    def __init__(self, current):
        self.current = current

    def now(self):
        return self.current

    def advance(self, **delta):
        self.current += timedelta(**delta)


class WebAuthTest(unittest.TestCase):
    def test_password_file_uses_scrypt_without_plaintext_and_private_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "web-auth.json"

            web_auth.write_password_config(path, "correct horse battery")

            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("correct horse battery", raw)
            self.assertTrue(web_auth.verify_password(path, "correct horse battery"))
            self.assertFalse(web_auth.verify_password(path, "wrong password"))
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_password_file_uses_a_private_temporary_file_before_replace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "web-auth.json"

            with mock.patch.object(web_auth.tempfile, "mkstemp", wraps=tempfile.mkstemp) as private_tempfile:
                web_auth.write_password_config(path, "correct horse battery")

            self.assertEqual(private_tempfile.call_count, 1)
            self.assertEqual(Path(private_tempfile.call_args.kwargs["dir"]), path.parent)
            self.assertEqual(path.read_text(encoding="utf-8").count("\n"), 1)

    def test_password_config_validation_rejects_missing_or_malformed_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "web-auth.json"

            self.assertFalse(web_auth.password_config_is_valid(path))
            path.write_text('{"version": 1, "kdf": "scrypt"}\n', encoding="utf-8")
            self.assertFalse(web_auth.password_config_is_valid(path))

            web_auth.write_password_config(path, "correct horse battery")
            self.assertTrue(web_auth.password_config_is_valid(path))

    def test_login_failure_rate_limit_locks_source_after_five_attempts(self):
        clock = MutableClock(datetime(2026, 7, 10, tzinfo=timezone.utc))
        store = web_auth.SessionStore(now=clock.now)

        for _ in range(4):
            self.assertFalse(store.record_login_failure("192.0.2.10"))
        self.assertTrue(store.record_login_failure("192.0.2.10"))
        self.assertTrue(store.is_login_locked("192.0.2.10"))

        clock.advance(minutes=15)
        self.assertFalse(store.is_login_locked("192.0.2.10"))

    def test_session_expires_after_idle_or_absolute_deadline(self):
        clock = MutableClock(datetime(2026, 7, 10, tzinfo=timezone.utc))
        store = web_auth.SessionStore(now=clock.now)
        session = store.create_session("192.0.2.20")

        self.assertIsNotNone(store.get_session(session.token))
        clock.advance(minutes=29)
        self.assertIsNotNone(store.get_session(session.token))
        clock.advance(minutes=31)
        self.assertIsNone(store.get_session(session.token))

        session = store.create_session("192.0.2.20")
        for _ in range(24):
            clock.advance(minutes=29)
            self.assertIsNotNone(store.get_session(session.token))
        clock.advance(minutes=24)
        self.assertIsNone(store.get_session(session.token))

    def test_login_csrf_is_bound_to_source_and_single_use(self):
        clock = MutableClock(datetime(2026, 7, 10, tzinfo=timezone.utc))
        store = web_auth.SessionStore(now=clock.now)
        token = store.issue_login_csrf("192.0.2.30")

        self.assertFalse(store.consume_login_csrf("192.0.2.31", token))
        self.assertTrue(store.consume_login_csrf("192.0.2.30", token))
        self.assertFalse(store.consume_login_csrf("192.0.2.30", token))

    def test_login_csrf_is_consumed_once_under_concurrent_requests(self):
        class CoordinatedSource:
            def __init__(self):
                self.barrier = threading.Barrier(2)

            def __ne__(self, other):
                try:
                    self.barrier.wait(timeout=0.1)
                except threading.BrokenBarrierError:
                    pass
                return False

        clock = MutableClock(datetime(2026, 7, 10, tzinfo=timezone.utc))
        store = web_auth.SessionStore(now=clock.now)
        source = CoordinatedSource()
        token = "one-time-login-token"
        store._login_csrf[token] = (source, clock.now() + web_auth.LOGIN_CSRF_TTL)
        results: list[bool] = []

        threads = [
            threading.Thread(target=lambda: results.append(store.consume_login_csrf(source, token)))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)


if __name__ == "__main__":
    unittest.main()
