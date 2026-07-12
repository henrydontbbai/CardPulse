#!/usr/bin/env python3
import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "lib"))

import cardpulse_web  # noqa: E402
from web_auth import write_password_config  # noqa: E402


class AuthRunner:
    def __init__(self):
        self.commands = []

    def run_cardpulse(self, args):
        self.commands.append(list(args))
        return cardpulse_web.CommandResult(0, "Storage: ME 1/23\nFormat: PDU\n", "")

    def run_at(self, cmd, timeout):
        return cardpulse_web.CommandResult(0, "OK", "")

    def send_message(self, phone, message):
        return cardpulse_web.CommandResult(0, "Message reference: 1", "")


class WebAuthIntegrationTest(unittest.TestCase):
    def start_server(self, history_records=None):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        auth_path = Path(tmpdir.name) / "config" / "web-auth.json"
        self.history_path = Path(tmpdir.name) / "state" / "messages.jsonl"
        self.history_audit_path = Path(tmpdir.name) / "state" / "local-history.jsonl"
        self.runner = AuthRunner()
        write_password_config(auth_path, "test-password")
        if history_records:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            self.history_path.write_text(
                "\n".join(json.dumps(record) for record in history_records) + "\n",
                encoding="utf-8",
            )
        handler = cardpulse_web.make_handler(
            runner=self.runner,
            ui_path=None,
            allow_sms=False,
            auth_path=auth_path,
            history_path=self.history_path,
            history_audit_path=self.history_audit_path,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.addCleanup(cleanup)
        return server

    def request(self, server, method, path, payload=None, headers=None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request_headers = dict(headers or {})
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(server.server_address[0], server.server_address[1], timeout=5)
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        raw = response.read().decode("utf-8")
        result = response.status, json.loads(raw), dict(response.getheaders())
        conn.close()
        return result

    def login(self, server):
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        status, session, headers = self.request(server, "GET", "/api/auth/session")
        self.assertEqual(status, 200)
        login_cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, login, headers = self.request(
            server,
            "POST",
            "/api/auth/login",
            {"password": "test-password"},
            {
                "Origin": origin,
                "X-CardPulse-CSRF": session["login_csrf_token"],
                "Cookie": login_cookie,
            },
        )
        self.assertEqual(status, 200)
        return origin, headers["Set-Cookie"].split(";", 1)[0], login["csrf_token"]

    def test_sensitive_api_requires_login_and_security_headers(self):
        server = self.start_server()

        status, data, headers = self.request(server, "GET", "/api/info")

        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_login_issues_secure_session_and_csrf_guards_post(self):
        server = self.start_server()
        origin, cookie, csrf = self.login(server)

        status, data, _ = self.request(server, "GET", "/api/info", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        status, health, _ = self.request(server, "GET", "/api/health", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertTrue(health["authenticated"])
        self.assertIn("started_at", health)
        self.assertIn("service_mode", health)
        self.assertIn("state_dir", health)

        status, data, _ = self.request(
            server,
            "POST",
            "/api/at",
            {"cmd": "AT"},
            {"Origin": origin, "Cookie": cookie},
        )
        self.assertEqual(status, 403)
        self.assertIn("CSRF", data["message"])

        status, data, _ = self.request(
            server,
            "POST",
            "/api/at",
            {"cmd": "AT"},
            {"Origin": "https://wrong.example", "Cookie": cookie, "X-CardPulse-CSRF": csrf},
        )
        self.assertEqual(status, 403)
        self.assertIn("Origin", data["message"])

        status, data, _ = self.request(
            server,
            "POST",
            "/api/at",
            {"cmd": "AT"},
            {"Origin": origin, "Cookie": cookie, "X-CardPulse-CSRF": csrf},
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_logout_invalidates_session(self):
        server = self.start_server()
        origin, cookie, csrf = self.login(server)

        status, data, _ = self.request(
            server,
            "POST",
            "/api/auth/logout",
            {},
            {"Origin": origin, "Cookie": cookie, "X-CardPulse-CSRF": csrf},
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        status, _, _ = self.request(server, "GET", "/api/info", headers={"Cookie": cookie})
        self.assertEqual(status, 401)

    def test_service_restart_invalidates_in_memory_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            auth_path = root / "config" / "web-auth.json"
            write_password_config(auth_path, "test-password")

            def start_restarted_handler():
                handler = cardpulse_web.make_handler(
                    runner=AuthRunner(),
                    ui_path=None,
                    allow_sms=False,
                    auth_path=auth_path,
                    history_path=root / "state" / "messages.jsonl",
                    history_audit_path=root / "state" / "local-history.jsonl",
                )
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                return server, thread

            first_server, first_thread = start_restarted_handler()
            origin, cookie, _ = self.login(first_server)
            self.assertEqual(origin, f"http://127.0.0.1:{first_server.server_address[1]}")
            first_server.shutdown()
            first_thread.join(timeout=2)
            first_server.server_close()

            second_server, second_thread = start_restarted_handler()
            try:
                status, _, _ = self.request(
                    second_server,
                    "GET",
                    "/api/info",
                    headers={"Cookie": cookie},
                )
                self.assertEqual(status, 401)
            finally:
                second_server.shutdown()
                second_thread.join(timeout=2)
                second_server.server_close()

    def test_forged_cookie_and_login_rate_limit_do_not_grant_access(self):
        server = self.start_server()
        origin = f"http://127.0.0.1:{server.server_address[1]}"

        status, _, _ = self.request(
            server,
            "GET",
            "/api/info",
            headers={"Cookie": "cardpulse_session=forged"},
        )
        self.assertEqual(status, 401)

        for attempt in range(5):
            status, session, headers = self.request(server, "GET", "/api/auth/session")
            self.assertEqual(status, 200)
            status, data, _ = self.request(
                server,
                "POST",
                "/api/auth/login",
                {"password": "wrong-password"},
                {
                    "Origin": origin,
                    "X-CardPulse-CSRF": session["login_csrf_token"],
                    "Cookie": headers["Set-Cookie"].split(";", 1)[0],
                },
            )
            self.assertEqual(status, 429 if attempt == 4 else 401)
            self.assertFalse(data["ok"])

        status, session, headers = self.request(server, "GET", "/api/auth/session")
        status, data, _ = self.request(
            server,
            "POST",
            "/api/auth/login",
            {"password": "test-password"},
            {
                "Origin": origin,
                "X-CardPulse-CSRF": session["login_csrf_token"],
                "Cookie": headers["Set-Cookie"].split(";", 1)[0],
            },
        )
        self.assertEqual(status, 429)
        self.assertFalse(data["ok"])

    def test_clear_local_history_requires_confirmation_and_never_calls_modem(self):
        server = self.start_server(
            history_records=[
                {
                    "id": "history-1",
                    "direction": "inbound",
                    "body": "private body",
                    "first_seen_at": "2026-07-10T00:00:00+00:00",
                }
            ]
        )
        origin, cookie, csrf = self.login(server)

        status, data, _ = self.request(
            server,
            "POST",
            "/api/messages/history/clear",
            {"confirm": "wrong"},
            {"Origin": origin, "Cookie": cookie, "X-CardPulse-CSRF": csrf},
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

        status, data, _ = self.request(
            server,
            "POST",
            "/api/messages/history/clear",
            {"confirm": "CLEAR_LOCAL_HISTORY"},
            {"Origin": origin, "Cookie": cookie, "X-CardPulse-CSRF": csrf},
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["cleared_count"], 1)
        self.assertEqual(self.history_path.read_text(encoding="utf-8"), "")
        self.assertEqual(self.runner.commands, [])
        audit = self.history_audit_path.read_text(encoding="utf-8")
        self.assertNotIn("private body", audit)
        self.assertNotIn("history-1", audit)


if __name__ == "__main__":
    unittest.main()
