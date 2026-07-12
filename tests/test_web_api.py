#!/usr/bin/env python3
import http.client
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parents[1]
POSIX_QDC507_TESTS = os.name == "posix" and hasattr(os, "makedev")
QDC507_IMAGE_REFERENCE = "registry.example/cardpulse@sha256:" + "a" * 64
sys.path.insert(0, str(ROOT_DIR / "lib"))

import cardpulse_web  # noqa: E402


class FakeRunner:
    def __init__(self):
        self.commands = []
        self.at_commands = []
        self.sent_messages = []
        self.notifications = []
        self.sms_indexes = {"1", "2"}
        self.storage_used = 23

    def run_cardpulse(self, args):
        self.commands.append(list(args))
        if args == ["--doctor"]:
            return cardpulse_web.CommandResult(
                0,
                "=== CardPulse doctor ===\n"
                "AT: OK\n"
                "SIM: READY\n"
                "RSSI: 21\n"
                "Network registration: 5\n"
                "Operator: CHINA MOBILE\n",
                "",
            )
        if args == ["--info"]:
            return cardpulse_web.CommandResult(
                0,
                "设备: /dev/ttyUSB2\n"
                "波特率: 115200\n"
                "厂商: Baiwang\n"
                "型号: QDC507\n"
                "IMEI: 863212060375703\n"
                "SIM 卡: READY\n"
                "信号强度: 21\n"
                "网络状态: 5\n"
                "运营商: CHINA MOBILE\n",
                "",
            )
        if args == ["--status"]:
            return cardpulse_web.CommandResult(
                0,
                "Last send: 2026-07-09 11:02:03\n"
                "Days since last send: 0\n"
                "Interval days: 179\n"
                "Remaining days: 179\n"
                "Send due: no\n"
                "Next send: 2027-01-04 11:02:03\n"
                "Last result: success\n",
                "",
            )
        if args == ["--sms-status"]:
            return cardpulse_web.CommandResult(
                0,
                f"Storage: ME {self.storage_used}/23"
                + (" FULL" if self.storage_used >= 23 else "")
                + "\n"
                "Format: PDU\n"
                "New message indication: 2,1,0,0,0",
                "",
            )
        if args == ["--inbox"]:
            records = []
            if "1" in self.sms_indexes:
                records.append(
                    "Index: 1\n"
                    "Status: REC READ\n"
                    "From: +8613025523391\n"
                    "Time: 2026-07-09 11:03:04\n"
                    "Preview: OK"
                )
            if "2" in self.sms_indexes:
                records.append(
                    "Index: 2\n"
                    "Status: REC UNREAD\n"
                    "From: +447700900123\n"
                    "Time: 2026-07-09 11:04:05\n"
                    "Preview: Hello from phone"
                )
            return cardpulse_web.CommandResult(
                0,
                "=== SMS inbox ===\n" + "\n\n".join(records) + ("\n" if records else ""),
                "",
            )
        if args == ["--read-sms", "1"]:
            return cardpulse_web.CommandResult(
                0,
                "=== SMS message ===\n"
                "Index: 1\n"
                "Status: REC READ\n"
                "From: +8613025523391\n"
                "Time: 2026-07-09 11:03:04\n"
                "Message: OK\n",
                "",
            )
        if args == ["--read-sms", "2"]:
            return cardpulse_web.CommandResult(
                0,
                "=== SMS message ===\n"
                "Index: 2\n"
                "Status: REC UNREAD\n"
                "From: +447700900123\n"
                "Time: 2026-07-09 11:04:05\n"
                "Message: Hello from phone\n",
                "",
            )
        if len(args) == 4 and args[0] == "--delete-sms" and args[2:] == ["--confirm", "DELETE_SMS"]:
            index = args[1]
            if index in self.sms_indexes:
                self.sms_indexes.remove(index)
                self.storage_used = max(0, self.storage_used - 1)
                return cardpulse_web.CommandResult(0, f"Deleted SMS index: {index}", "")
            return cardpulse_web.CommandResult(4, "", f"[ERROR] SMS index {index} not found")
        return cardpulse_web.CommandResult(0, "ran " + " ".join(args), "")

    def run_at(self, cmd, timeout):
        self.at_commands.append((cmd, timeout))
        return cardpulse_web.CommandResult(0, "OK", "")

    def send_message(self, phone, message):
        self.sent_messages.append((phone, message))
        return cardpulse_web.CommandResult(0, "Message reference: 42", "")

    def notify_event(self, title, body):
        self.notifications.append((title, body))
        return cardpulse_web.CommandResult(0, "notify ok", "")


class FailingSmsStatusRunner(FakeRunner):
    def run_cardpulse(self, args):
        if args == ["--sms-status"]:
            self.commands.append(list(args))
            return cardpulse_web.CommandResult(2, "", "[ERROR] sms storage did not respond")
        return super().run_cardpulse(args)


class RecoveringInfoRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.fail_info = True

    def run_cardpulse(self, args):
        if args == ["--info"] and self.fail_info:
            self.commands.append(list(args))
            return cardpulse_web.CommandResult(2, "", "[ERROR] info failed")
        return super().run_cardpulse(args)


class UnparseableReadSmsRunner(FakeRunner):
    def run_cardpulse(self, args):
        if args == ["--read-sms", "9"]:
            self.commands.append(list(args))
            return cardpulse_web.CommandResult(0, "raw modem output without message fields", "")
        return super().run_cardpulse(args)


class RotatingInboxRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.inbox_calls = 0
        self.rotating_indexes = {"1", "2"}

    def run_cardpulse(self, args):
        if args == ["--inbox"]:
            self.commands.append(list(args))
            self.inbox_calls += 1
            if self.inbox_calls == 1:
                output = (
                    "=== SMS inbox ===\n"
                    "Index: 1\n"
                    "Status: REC READ\n"
                    "From: +8613025523391\n"
                    "Time: 2026-07-09 11:03:04\n"
                    "Preview: Old message\n"
                )
            else:
                records = [
                    "=== SMS inbox ===\n"
                    "Index: 1\n"
                    "Status: REC READ\n"
                    "From: +8613025523391\n"
                    "Time: 2026-07-09 11:03:04\n"
                    "Preview: Old message"
                ]
                if "2" in self.rotating_indexes:
                    records.append(
                        "Index: 2\n"
                        "Status: REC UNREAD\n"
                        "From: +447700900123\n"
                        "Time: 2026-07-09 12:10:11\n"
                        "Preview: Brand new inbound"
                    )
                output = "\n\n".join(records) + "\n"
            return cardpulse_web.CommandResult(0, output, "")
        if args == ["--sms-status"]:
            self.commands.append(list(args))
            return cardpulse_web.CommandResult(
                0,
                "Storage: ME 20/23\n"
                "Format: PDU\n"
                "New message indication: 2,1,0,0,0",
                "",
            )
        if args == ["--delete-sms", "2", "--confirm", "DELETE_SMS"]:
            self.commands.append(list(args))
            self.rotating_indexes.discard("2")
            self.sms_indexes.discard("2")
            self.storage_used = max(0, self.storage_used - 1)
            return cardpulse_web.CommandResult(0, "Deleted SMS index: 2", "")
        return super().run_cardpulse(args)


class FailingDeleteRunner(RotatingInboxRunner):
    def run_cardpulse(self, args):
        if args == ["--delete-sms", "2", "--confirm", "DELETE_SMS"]:
            self.commands.append(list(args))
            return cardpulse_web.CommandResult(3, "", "[ERROR] delete failed")
        return super().run_cardpulse(args)


class MutableSmsStatusRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.sms_status_output = (
            "Storage: ME 23/23 FULL\n"
            "Format: PDU\n"
            "New message indication: 2,1,0,0,0"
        )

    def run_cardpulse(self, args):
        if args == ["--sms-status"]:
            self.commands.append(list(args))
            return cardpulse_web.CommandResult(0, self.sms_status_output, "")
        return super().run_cardpulse(args)


class MultipartInboxRunner(FakeRunner):
    def __init__(self, *, full=False, invalid_time=False):
        super().__init__()
        self.full = full
        self.invalid_time = invalid_time
        self.deleted_indexes = []
        self.active_indexes = {"1", "2", "3", "4", "5", "6", "7"}

    def inbox_output(self):
        records = []
        oldest_time = "unknown" if self.invalid_time else "2024-01-01 00:00:00"
        if "1" in self.active_indexes:
            records.append(
                "Index: 1\n"
                "Status: REC READ\n"
                "From: +10000000001\n"
                f"Time: {oldest_time}\n"
                "Preview: Oldest single"
            )
        if {"2", "3"}.issubset(self.active_indexes):
            records.append(
                "Indexes: 2,3\n"
                "Status: REC READ\n"
                "From: +10000000002\n"
                "Time: 2024-01-02 00:00:00\n"
                "Parts: 2/2\n"
                "Preview: Older multipart\n\n"
                "message body continues"
            )
        if {"4", "5", "6"}.issubset(self.active_indexes):
            records.append(
                "Indexes: 4,5,6\n"
                "Status: REC READ\n"
                "From: +10000000003\n"
                "Time: 2024-01-03 00:00:00\n"
                "Parts: 3/3\n"
                "Preview: Boundary multipart"
            )
        if "7" in self.active_indexes:
            records.append(
                "Index: 7\n"
                "Status: REC READ\n"
                "From: +10000000007\n"
                "Time: 2025-01-01 00:00:00\n"
                "Preview: Newest single"
            )
        return "=== SMS inbox ===\n" + "\n\n".join(records) + ("\n" if records else "")

    def run_cardpulse(self, args):
        if args == ["--sms-status"]:
            self.commands.append(list(args))
            storage = "ME 23/23 FULL" if self.full else "ME 19/23"
            return cardpulse_web.CommandResult(
                0,
                f"Storage: {storage}\nFormat: PDU\nNew message indication: 2,1,0,0,0",
                "",
            )
        if args == ["--inbox"]:
            self.commands.append(list(args))
            return cardpulse_web.CommandResult(
                0,
                self.inbox_output(),
                "[WARN] stty failed for /dev/ttyUSB2; continuing with existing serial settings",
            )
        if len(args) == 4 and args[0] == "--delete-sms" and args[2:] == ["--confirm", "DELETE_SMS"]:
            self.commands.append(list(args))
            self.deleted_indexes.append(args[1])
            self.active_indexes.discard(args[1])
            return cardpulse_web.CommandResult(0, f"Deleted SMS index: {args[1]}", "")
        return super().run_cardpulse(args)


class FailingBatchDeleteRunner(MultipartInboxRunner):
    def run_cardpulse(self, args):
        if args == ["--delete-sms", "5", "--confirm", "DELETE_SMS"]:
            self.commands.append(list(args))
            self.deleted_indexes.append("5")
            return cardpulse_web.CommandResult(4, "", "[ERROR] delete failed")
        return super().run_cardpulse(args)


class IncompleteMultipartRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.deleted_indexes = []
        self.active_indexes = {"17"}

    def run_cardpulse(self, args):
        if args == ["--inbox"]:
            self.commands.append(list(args))
            return cardpulse_web.CommandResult(
                0,
                (
                    "=== SMS inbox ===\n"
                    "Indexes: 17\n"
                    "Status: REC READ\n"
                    "From: 10655777\n"
                    "Time: 2024-11-20 17:07:00\n"
                    "Parts: 1/2\n"
                    "Preview: Residual concatenated message\n"
                    if "17" in self.active_indexes
                    else "=== SMS inbox ===\n"
                ),
                "",
            )
        if args == ["--delete-sms", "17", "--confirm", "DELETE_SMS"]:
            self.commands.append(list(args))
            self.deleted_indexes.append("17")
            self.active_indexes.discard("17")
            return cardpulse_web.CommandResult(0, "Deleted SMS index: 17", "")
        return super().run_cardpulse(args)


class FailingIncompleteMultipartDeleteRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.deleted_indexes = []
        self.active_indexes = {"17", "18"}

    def run_cardpulse(self, args):
        if args == ["--inbox"]:
            self.commands.append(list(args))
            if not self.active_indexes:
                return cardpulse_web.CommandResult(0, "=== SMS inbox ===\n", "")
            return cardpulse_web.CommandResult(
                0,
                "=== SMS inbox ===\n"
                "Indexes: 17,18\n"
                "Status: REC READ\n"
                "From: 10655777\n"
                "Time: 2024-11-20 17:07:00\n"
                "Parts: 2/3\n"
                "Preview: Residual concatenated message\n",
                "",
            )
        if args == ["--delete-sms", "18", "--confirm", "DELETE_SMS"]:
            self.commands.append(list(args))
            self.deleted_indexes.append("18")
            self.active_indexes.discard("18")
            return cardpulse_web.CommandResult(0, "Deleted SMS index: 18", "")
        if args == ["--delete-sms", "17", "--confirm", "DELETE_SMS"]:
            self.commands.append(list(args))
            self.deleted_indexes.append("17")
            return cardpulse_web.CommandResult(4, "", "[ERROR] delete failed")
        return super().run_cardpulse(args)


class StaleDeleteRunner(FakeRunner):
    def run_cardpulse(self, args):
        if args == ["--delete-sms", "2", "--confirm", "DELETE_SMS"]:
            self.commands.append(list(args))
            return cardpulse_web.CommandResult(0, "Deleted SMS index: 2", "")
        return super().run_cardpulse(args)


class VerificationSmsStatusFailureRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.sms_status_calls = 0

    def run_cardpulse(self, args):
        if args == ["--sms-status"]:
            self.commands.append(list(args))
            self.sms_status_calls += 1
            if self.sms_status_calls == 1:
                return cardpulse_web.CommandResult(2, "", "[ERROR] SMS storage read failed")
            return super().run_cardpulse(args)
        return super().run_cardpulse(args)


class VerificationMalformedInboxRunner(FakeRunner):
    def run_cardpulse(self, args):
        if args == ["--inbox"]:
            self.commands.append(list(args))
            return cardpulse_web.CommandResult(
                0,
                "modem returned success without the SMS inbox contract",
                "",
            )
        return super().run_cardpulse(args)


class HeaderlessBatchInboxRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.inbox_calls = 0

    def run_cardpulse(self, args):
        if args == ["--inbox"]:
            self.inbox_calls += 1
            if self.inbox_calls == 1:
                self.commands.append(list(args))
                return cardpulse_web.CommandResult(
                    0,
                    (
                        "Index: 2\n"
                        "Status: REC UNREAD\n"
                        "From: +447700900123\n"
                        "Time: 2026-07-09 11:04:05\n"
                        "Preview: Headerless modem output\n"
                    ),
                    "",
                )
        return super().run_cardpulse(args)


class VerificationMalformedSmsStatusRunner(FakeRunner):
    def run_cardpulse(self, args):
        if args == ["--sms-status"]:
            self.commands.append(list(args))
            return cardpulse_web.CommandResult(
                0,
                "Storage: unknown\nFormat: PDU\n",
                "",
            )
        return super().run_cardpulse(args)


class AmbiguousIncompleteMultipartRunner(IncompleteMultipartRunner):
    def run_cardpulse(self, args):
        if args == ["--inbox"]:
            self.commands.append(list(args))
            return cardpulse_web.CommandResult(
                0,
                "=== SMS inbox ===\n"
                "Indexes: 17\n"
                "Status: REC READ\n"
                "From: 10655777\n"
                "Time: 2024-11-20 17:07:00\n"
                "Parts: 1/2\n"
                "Preview: Residual concatenated message\n\n"
                "Indexes: 17,18\n"
                "Status: REC READ\n"
                "From: 10655778\n"
                "Time: 2024-11-21 17:07:00\n"
                "Parts: 2/2\n"
                "Preview: Conflicting parser output\n",
                "",
            )
        return super().run_cardpulse(args)


class HeaderlessIncompleteMultipartRunner(IncompleteMultipartRunner):
    def __init__(self):
        super().__init__()
        self.inbox_calls = 0

    def run_cardpulse(self, args):
        if args == ["--inbox"]:
            self.inbox_calls += 1
            if self.inbox_calls == 1:
                self.commands.append(list(args))
                return cardpulse_web.CommandResult(
                    0,
                    (
                        "Indexes: 17\n"
                        "Status: REC READ\n"
                        "From: 10655777\n"
                        "Time: 2024-11-20 17:07:00\n"
                        "Parts: 1/2\n"
                        "Preview: Headerless incomplete multipart\n"
                    ),
                    "",
                )
        return super().run_cardpulse(args)


class SerialOperationRunner(MultipartInboxRunner):
    def __init__(self):
        super().__init__(full=True)
        self._active = 0
        self._max_active = 0
        self._activity_lock = threading.Lock()

    def run_cardpulse(self, args):
        with self._activity_lock:
            self._active += 1
            self._max_active = max(self._max_active, self._active)
        try:
            time.sleep(0.03)
            return super().run_cardpulse(args)
        finally:
            with self._activity_lock:
                self._active -= 1

    @property
    def max_active(self):
        with self._activity_lock:
            return self._max_active


class TimeoutCleanupRunner(FakeRunner):
    def run_cardpulse(self, args):
        if args == ["--sms-status"]:
            raise subprocess.TimeoutExpired(["bash", "bin/cardpulse", "--sms-status"], 45)
        return super().run_cardpulse(args)


class WebAPITestCase(unittest.TestCase):
    def start_server(
        self,
        allow_sms=False,
        runner=None,
        history_path=None,
        ops_state_path=None,
        recovery_state_path=None,
        operations_audit_path=None,
        scheduler_config_path=None,
        qdc507_acceptance_path=None,
    ):
        runner = runner or FakeRunner()
        handler = cardpulse_web.make_handler(
            runner=runner,
            ui_path=None,
            allow_sms=allow_sms,
            history_path=history_path,
            ops_state_path=ops_state_path,
            recovery_state_path=recovery_state_path,
            operations_audit_path=operations_audit_path,
            scheduler_config_path=scheduler_config_path,
            qdc507_acceptance_path=qdc507_acceptance_path,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.addCleanup(cleanup)
        return server, runner

    def request(self, server, method, path, payload=None):
        conn = http.client.HTTPConnection(server.server_address[0], server.server_address[1], timeout=5)
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read().decode("utf-8")
        conn.close()
        return response.status, json.loads(data)

    def raw_request(self, server, method, path, *, headers=None):
        conn = http.client.HTTPConnection(server.server_address[0], server.server_address[1], timeout=5)
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        response_headers = dict(response.getheaders())
        body = response.read().decode("utf-8")
        conn.close()
        return response.status, response_headers, body

    def test_health_reports_sms_gate(self):
        server, _ = self.start_server(allow_sms=False)

        status, data = self.request(server, "GET", "/api/health")

        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["sms_enabled"])
        self.assertNotIn("auto_cleanup_oldest_on_full", data)

    def test_scheduler_refuses_enable_without_explicit_sms_configuration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "serial:\n  port: /dev/cardpulse-at\n  auto_detect: false\n"
                "sms:\n  phone: ''\n  message: ''\n  interval_days: 179\n"
                "scheduler:\n  enabled: false\n",
                encoding="utf-8",
            )
            server, _ = self.start_server(scheduler_config_path=config_path)

            status, data = self.request(
                server,
                "POST",
                "/api/scheduler",
                {"enabled": True, "confirm": "SET_SCHEDULER_ENABLED"},
            )

            self.assertEqual(status, 409)
            self.assertFalse(data["enabled"])
            self.assertIn("recipient", data["message"])
            self.assertFalse(cardpulse_web.scheduler_enabled_from_config(config_path))

    @unittest.skipUnless(POSIX_QDC507_TESTS, "requires POSIX QDC507 device metadata")
    def test_keepalive_config_can_be_saved_before_enabling_scheduler(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "serial:\n  port: /dev/cardpulse-at\n  auto_detect: false\n"
                "sms:\n  phone: ''\n  message: ''\n  interval_days: 179\n"
                "scheduler:\n  enabled: false\n",
                encoding="utf-8",
            )
            acceptance_path = Path(tmpdir) / "qdc507-readonly-acceptance.json"
            acceptance_path.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "device": "/dev/cardpulse-at",
                        "resolved_device": "/dev/ttyUSB2",
                        "usb_id": "2ca3:4006",
                        "runtime_version": "1.1.0",
                        "image_reference": QDC507_IMAGE_REFERENCE,
                        "package_version": "1.1.0",
                        "commands": ["--doctor", "--info", "--sms-status", "--status"],
                        "nonroot": True,
                        "socket_backend": True,
                        "device_mode": "enabled",
                        "completed_at": "2026-07-11T20:00:00+08:00",
                    }
                ),
                encoding="utf-8",
            )
            acceptance_path.chmod(0o600)
            (acceptance_path.parent / "runtime-version").write_text("1.1.0\n", encoding="utf-8")
            (acceptance_path.parent / "qdc507-device-mode.active").write_text(
                "enabled\n", encoding="utf-8"
            )
            sysfs_tty_root = self.write_qdc507_sysfs_fixture(acceptance_path.parent / "sys", "ttyUSB2", "2ca3:4006")
            server, _ = self.start_server(
                scheduler_config_path=config_path,
                qdc507_acceptance_path=acceptance_path,
            )

            status, data = self.request(
                server,
                "POST",
                "/api/keepalive-config",
                {"phone": "+8613800138000", "message": "CardPulse keepalive", "interval_days": 179},
            )

            self.assertEqual(status, 200)
            self.assertTrue(data["ready"])
            self.assertEqual(data["phone"], "+8613800138000")
            self.assertEqual(data["message"], "CardPulse keepalive")

            with self.qdc507_current_device_patch(sysfs_tty_root):
                status, data = self.request(
                    server,
                    "POST",
                    "/api/scheduler",
                    {"enabled": True, "confirm": "SET_SCHEDULER_ENABLED"},
                )

            self.assertEqual(status, 200)
            self.assertTrue(data["enabled"])

    def test_scheduler_refuses_enable_without_qdc507_readonly_acceptance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "serial:\n  port: /dev/cardpulse-at\n  auto_detect: false\n"
                "sms:\n  phone: '+8613800138000'\n  message: 'CardPulse keepalive'\n  interval_days: 179\n"
                "scheduler:\n  enabled: false\n",
                encoding="utf-8",
            )
            acceptance_path = Path(tmpdir) / "qdc507-readonly-acceptance.json"
            server, _ = self.start_server(
                scheduler_config_path=config_path,
                qdc507_acceptance_path=acceptance_path,
            )

            status, data = self.request(
                server,
                "POST",
                "/api/scheduler",
                {"enabled": True, "confirm": "SET_SCHEDULER_ENABLED"},
            )

            self.assertEqual(status, 409)
            self.assertFalse(data["enabled"])
            self.assertIn("QDC507", data["message"])
            self.assertFalse(cardpulse_web.scheduler_enabled_from_config(config_path))

    @unittest.skipUnless(os.name == "posix", "requires POSIX QDC507 device metadata")
    def test_fnos_acceptance_rejects_runtime_writable_state_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            state_dir = data_dir / "state"
            state_dir.mkdir(parents=True)
            acceptance_path = self.write_qdc507_acceptance_marker(state_dir)
            sysfs_tty_root = self.write_qdc507_sysfs_fixture(
                data_dir / "sys", "ttyUSB2", "2ca3:4006"
            )

            with mock.patch.dict(
                os.environ,
                {
                    "CARDPULSE_FNOS_RUNTIME": "1",
                    "CARDPULSE_DATA_DIR": str(data_dir),
                },
                clear=False,
            ):
                accepted, reason = self.qdc507_status_with_current_device(
                    acceptance_path, sysfs_tty_root
                )

            self.assertFalse(accepted)
            self.assertIn("lifecycle", reason)

    @unittest.skipUnless(os.name == "posix", "requires POSIX QDC507 device metadata")
    def test_fnos_acceptance_accepts_lifecycle_owned_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            lifecycle_dir = data_dir / "lifecycle"
            state_dir = data_dir / "state"
            lifecycle_dir.mkdir(parents=True)
            state_dir.mkdir()
            data_dir.chmod(0o3770)
            lifecycle_dir.chmod(0o2750)
            acceptance_path = self.write_qdc507_acceptance_marker(lifecycle_dir)
            acceptance_path.chmod(0o640)
            (lifecycle_dir / "runtime-version").unlink()
            (state_dir / "runtime-version").write_text("forged-state-version\n", encoding="utf-8")
            sysfs_tty_root = self.write_qdc507_sysfs_fixture(
                data_dir / "sys", "ttyUSB2", "2ca3:4006"
            )

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "CARDPULSE_FNOS_RUNTIME": "1",
                        "CARDPULSE_DATA_DIR": str(data_dir),
                        "CARDPULSE_RUNTIME_VERSION": "1.1.1",
                        "CARDPULSE_RUNTIME_IMAGE": QDC507_IMAGE_REFERENCE,
                        "CARDPULSE_FPK_VERSION": "1.1.1",
                    },
                    clear=False,
                ),
                mock.patch.object(cardpulse_web.os, "getuid", return_value=os.getuid() + 1),
            ):
                accepted, reason = self.qdc507_status_with_current_device(
                    acceptance_path, sysfs_tty_root
                )

            self.assertTrue(accepted, reason)

    @unittest.skipUnless(os.name == "posix", "requires POSIX QDC507 device metadata")
    def test_fnos_acceptance_rejects_a_changed_image_runtime_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            lifecycle_dir = data_dir / "lifecycle"
            lifecycle_dir.mkdir(parents=True)
            data_dir.chmod(0o3770)
            lifecycle_dir.chmod(0o2750)
            acceptance_path = self.write_qdc507_acceptance_marker(lifecycle_dir)
            acceptance_path.chmod(0o640)
            (lifecycle_dir / "runtime-version").unlink()
            sysfs_tty_root = self.write_qdc507_sysfs_fixture(
                data_dir / "sys", "ttyUSB2", "2ca3:4006"
            )

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "CARDPULSE_FNOS_RUNTIME": "1",
                        "CARDPULSE_DATA_DIR": str(data_dir),
                        "CARDPULSE_RUNTIME_VERSION": "1.1.2",
                        "CARDPULSE_RUNTIME_IMAGE": QDC507_IMAGE_REFERENCE,
                        "CARDPULSE_FPK_VERSION": "1.1.1",
                    },
                    clear=False,
                ),
                mock.patch.object(cardpulse_web.os, "getuid", return_value=os.getuid() + 1),
            ):
                accepted, reason = self.qdc507_status_with_current_device(
                    acceptance_path, sysfs_tty_root
                )

            self.assertFalse(accepted)
            self.assertIn("runtime version has changed", reason)

    @unittest.skipUnless(os.name == "posix", "requires POSIX QDC507 device metadata")
    def test_fnos_acceptance_rejects_a_changed_runtime_image_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            lifecycle_dir = data_dir / "lifecycle"
            lifecycle_dir.mkdir(parents=True)
            data_dir.chmod(0o3770)
            lifecycle_dir.chmod(0o2750)
            acceptance_path = self.write_qdc507_acceptance_marker(lifecycle_dir)
            acceptance_path.chmod(0o640)
            (lifecycle_dir / "runtime-version").unlink()
            sysfs_tty_root = self.write_qdc507_sysfs_fixture(
                data_dir / "sys", "ttyUSB2", "2ca3:4006"
            )
            changed_image = "registry.example/cardpulse@sha256:" + "b" * 64

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "CARDPULSE_FNOS_RUNTIME": "1",
                        "CARDPULSE_DATA_DIR": str(data_dir),
                        "CARDPULSE_RUNTIME_VERSION": "1.1.1",
                        "CARDPULSE_RUNTIME_IMAGE": changed_image,
                        "CARDPULSE_FPK_VERSION": "1.1.1",
                    },
                    clear=False,
                ),
                mock.patch.object(cardpulse_web.os, "getuid", return_value=os.getuid() + 1),
            ):
                accepted, reason = self.qdc507_status_with_current_device(
                    acceptance_path, sysfs_tty_root
                )

            self.assertFalse(accepted)
            self.assertIn("runtime image has changed", reason)

    @unittest.skipUnless(os.name == "posix", "requires POSIX QDC507 device metadata")
    def test_fnos_acceptance_rejects_a_changed_fpk_package_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            lifecycle_dir = data_dir / "lifecycle"
            lifecycle_dir.mkdir(parents=True)
            data_dir.chmod(0o3770)
            lifecycle_dir.chmod(0o2750)
            acceptance_path = self.write_qdc507_acceptance_marker(lifecycle_dir)
            acceptance_path.chmod(0o640)
            (lifecycle_dir / "runtime-version").unlink()
            sysfs_tty_root = self.write_qdc507_sysfs_fixture(
                data_dir / "sys", "ttyUSB2", "2ca3:4006"
            )

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "CARDPULSE_FNOS_RUNTIME": "1",
                        "CARDPULSE_DATA_DIR": str(data_dir),
                        "CARDPULSE_RUNTIME_VERSION": "1.1.1",
                        "CARDPULSE_RUNTIME_IMAGE": QDC507_IMAGE_REFERENCE,
                        "CARDPULSE_FPK_VERSION": "1.1.2",
                    },
                    clear=False,
                ),
                mock.patch.object(cardpulse_web.os, "getuid", return_value=os.getuid() + 1),
            ):
                accepted, reason = self.qdc507_status_with_current_device(
                    acceptance_path, sysfs_tty_root
                )

            self.assertFalse(accepted)
            self.assertIn("package version has changed", reason)

    @unittest.skipUnless(os.name == "posix", "requires POSIX QDC507 device metadata")
    def test_fnos_acceptance_rejects_runtime_owned_lifecycle_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            lifecycle_dir = data_dir / "lifecycle"
            lifecycle_dir.mkdir(parents=True)
            data_dir.chmod(0o3770)
            lifecycle_dir.chmod(0o2750)
            acceptance_path = self.write_qdc507_acceptance_marker(lifecycle_dir)
            acceptance_path.chmod(0o640)
            sysfs_tty_root = self.write_qdc507_sysfs_fixture(
                data_dir / "sys", "ttyUSB2", "2ca3:4006"
            )

            with mock.patch.dict(
                os.environ,
                {
                    "CARDPULSE_FNOS_RUNTIME": "1",
                    "CARDPULSE_DATA_DIR": str(data_dir),
                },
                clear=False,
            ):
                accepted, reason = self.qdc507_status_with_current_device(
                    acceptance_path, sysfs_tty_root
                )

            self.assertFalse(accepted)
            self.assertIn("lifecycle", reason)

    @unittest.skipUnless(POSIX_QDC507_TESTS, "requires POSIX QDC507 device metadata")
    def test_qdc507_acceptance_binds_device_identity_runtime_and_active_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            acceptance_path = self.write_qdc507_acceptance_marker(state_dir)
            sysfs_tty_root = self.write_qdc507_sysfs_fixture(state_dir / "sys", "ttyUSB2", "2ca3:4006")

            accepted, reason = self.qdc507_status_with_current_device(acceptance_path, sysfs_tty_root)

            self.assertTrue(accepted, reason)
            (state_dir / "runtime-version").write_text("1.1.2\n", encoding="utf-8")
            accepted, reason = self.qdc507_status_with_current_device(acceptance_path, sysfs_tty_root)
            self.assertFalse(accepted)
            self.assertIn("runtime version", reason)

            (state_dir / "runtime-version").write_text("1.1.1\n", encoding="utf-8")
            acceptance_path.write_text(
                acceptance_path.read_text(encoding="utf-8").replace("2ca3:4006", "ffff:ffff"),
                encoding="utf-8",
            )
            acceptance_path.chmod(0o600)
            accepted, reason = self.qdc507_status_with_current_device(acceptance_path, sysfs_tty_root)
            self.assertFalse(accepted)
            self.assertIn("USB identity", reason)

    @unittest.skipUnless(POSIX_QDC507_TESTS, "requires POSIX QDC507 device metadata")
    def test_qdc507_acceptance_rejects_missing_current_device_sysfs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            acceptance_path = self.write_qdc507_acceptance_marker(state_dir)
            missing_sysfs_root = state_dir / "missing-sysfs"

            accepted, reason = self.qdc507_status_with_current_device(acceptance_path, missing_sysfs_root)

            self.assertFalse(accepted)
            self.assertIn("current device", reason)

    @unittest.skipUnless(POSIX_QDC507_TESTS, "requires POSIX QDC507 device metadata")
    def test_qdc507_acceptance_rejects_current_alias_resolved_device_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            acceptance_path = self.write_qdc507_acceptance_marker(state_dir)
            sysfs_tty_root = self.write_qdc507_sysfs_fixture(state_dir / "sys", "ttyUSB3", "2ca3:4006")

            accepted, reason = self.qdc507_status_with_current_device(acceptance_path, sysfs_tty_root)

            self.assertFalse(accepted)
            self.assertIn("resolved device", reason)

    @unittest.skipUnless(POSIX_QDC507_TESTS, "requires POSIX QDC507 device metadata")
    def test_qdc507_acceptance_rejects_current_alias_usb_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            acceptance_path = self.write_qdc507_acceptance_marker(state_dir)
            sysfs_tty_root = self.write_qdc507_sysfs_fixture(state_dir / "sys", "ttyUSB2", "ffff:ffff")

            accepted, reason = self.qdc507_status_with_current_device(acceptance_path, sysfs_tty_root)

            self.assertFalse(accepted)
            self.assertIn("current device USB identity", reason)

    @unittest.skipUnless(POSIX_QDC507_TESTS, "requires POSIX QDC507 device metadata")
    def test_qdc507_acceptance_uses_protected_lifecycle_device_mode_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            state_dir = data_dir / "state"
            state_dir.mkdir(parents=True)
            acceptance_path = self.write_qdc507_acceptance_marker(state_dir)
            (state_dir / "qdc507-device-mode.active").write_text("degraded\n", encoding="utf-8")
            protected_active_path = data_dir / "lifecycle" / "qdc507-device-mode.active"
            protected_active_path.parent.mkdir()
            protected_active_path.write_text("enabled\n", encoding="utf-8")
            sysfs_tty_root = self.write_qdc507_sysfs_fixture(data_dir / "sys", "ttyUSB2", "2ca3:4006")

            with mock.patch.dict(
                os.environ,
                {"CARDPULSE_QDC507_DEVICE_MODE_PATH": str(protected_active_path)},
            ):
                accepted, reason = self.qdc507_status_with_current_device(acceptance_path, sysfs_tty_root)

            self.assertTrue(accepted, reason)

    @unittest.skipUnless(POSIX_QDC507_TESTS, "requires POSIX QDC507 device metadata")
    def test_qdc507_acceptance_rejects_symlinked_lifecycle_device_mode_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            state_dir = data_dir / "state"
            state_dir.mkdir(parents=True)
            acceptance_path = self.write_qdc507_acceptance_marker(state_dir)
            protected_active_path = data_dir / "lifecycle" / "qdc507-device-mode.active"
            protected_active_path.parent.mkdir()
            sentinel = data_dir / "sentinel"
            sentinel.write_text("enabled\n", encoding="utf-8")
            protected_active_path.symlink_to(sentinel)
            sysfs_tty_root = self.write_qdc507_sysfs_fixture(data_dir / "sys", "ttyUSB2", "2ca3:4006")

            with mock.patch.dict(
                os.environ,
                {"CARDPULSE_QDC507_DEVICE_MODE_PATH": str(protected_active_path)},
            ):
                accepted, reason = self.qdc507_status_with_current_device(acceptance_path, sysfs_tty_root)

            self.assertFalse(accepted)
            self.assertIn("device mode is invalid", reason)

    def write_qdc507_acceptance_marker(self, state_dir):
        acceptance_path = state_dir / "qdc507-readonly-acceptance.json"
        acceptance_path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "device": "/dev/cardpulse-at",
                    "resolved_device": "/dev/ttyUSB2",
                    "usb_id": "2ca3:4006",
                    "runtime_version": "1.1.1",
                    "image_reference": QDC507_IMAGE_REFERENCE,
                    "package_version": "1.1.1",
                    "commands": ["--doctor", "--info", "--sms-status", "--status"],
                    "nonroot": True,
                    "socket_backend": True,
                    "device_mode": "enabled",
                    "completed_at": "2026-07-11T20:00:00+08:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        acceptance_path.chmod(0o600)
        (state_dir / "runtime-version").write_text("1.1.1\n", encoding="utf-8")
        (state_dir / "qdc507-device-mode.active").write_text("enabled\n", encoding="utf-8")
        return acceptance_path

    def write_qdc507_sysfs_fixture(self, root, tty_name, usb_id):
        sysfs_tty_root = root / "class" / "tty"
        tty_path = sysfs_tty_root / tty_name
        tty_path.mkdir(parents=True)
        (tty_path / "dev").write_text("188:2\n", encoding="utf-8")
        vendor, product = usb_id.split(":", 1)
        root.mkdir(parents=True, exist_ok=True)
        (root / "idVendor").write_text(vendor + "\n", encoding="utf-8")
        (root / "idProduct").write_text(product + "\n", encoding="utf-8")
        return sysfs_tty_root

    def qdc507_status_with_current_device(self, acceptance_path, sysfs_tty_root):
        with self.qdc507_current_device_patch(sysfs_tty_root):
            return cardpulse_web.qdc507_readonly_acceptance_status(acceptance_path)

    @contextmanager
    def qdc507_current_device_patch(self, sysfs_tty_root):
        real_stat = os.stat
        device_stat = mock.Mock(st_mode=stat.S_IFCHR | 0o660, st_rdev=os.makedev(188, 2))

        def stat_with_cardpulse_alias(path, *args, **kwargs):
            if os.fspath(path) == cardpulse_web.QDC507_ACCEPTANCE_DEVICE:
                return device_stat
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(cardpulse_web, "QDC507_SYSFS_TTY_ROOT", sysfs_tty_root, create=True):
            with mock.patch.object(cardpulse_web.os, "stat", side_effect=stat_with_cardpulse_alias):
                yield

    def test_scheduler_refuses_enable_with_invalid_qdc507_readonly_acceptance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "serial:\n  port: /dev/cardpulse-at\n  auto_detect: false\n"
                "sms:\n  phone: '+8613800138000'\n  message: 'CardPulse keepalive'\n  interval_days: 179\n"
                "scheduler:\n  enabled: false\n",
                encoding="utf-8",
            )
            acceptance_path = Path(tmpdir) / "qdc507-readonly-acceptance.json"
            acceptance_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "device": "/dev/ttyUSB2",
                        "commands": ["--doctor", "--info", "--sms-status", "--status"],
                        "nonroot": True,
                        "gateway": True,
                        "completed_at": "2026-07-11T20:00:00+08:00",
                    }
                ),
                encoding="utf-8",
            )
            server, _ = self.start_server(
                scheduler_config_path=config_path,
                qdc507_acceptance_path=acceptance_path,
            )

            status, current = self.request(server, "GET", "/api/scheduler")
            self.assertEqual(status, 200)
            self.assertFalse(current["accepted"])
            self.assertIn("QDC507", current["reason"])

            status, data = self.request(
                server,
                "POST",
                "/api/scheduler",
                {"enabled": True, "confirm": "SET_SCHEDULER_ENABLED"},
            )

            self.assertEqual(status, 409)
            self.assertFalse(data["enabled"])
            self.assertIn("QDC507", data["message"])
            self.assertFalse(cardpulse_web.scheduler_enabled_from_config(config_path))

    def test_gateway_base_path_rejects_unprefixed_requests(self):
        runner = FakeRunner()
        handler = cardpulse_web.make_handler(
            runner=runner,
            ui_path=None,
            allow_sms=False,
            base_path="/app/cardpulse",
            gateway_admin_only=True,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.addCleanup(cleanup)

        status, data = self.request(server, "GET", "/api/health")

        self.assertEqual(status, 404)
        self.assertEqual(data["message"], "not found")

    def test_gateway_allows_same_origin_iframe_for_administrator_path(self):
        handler = cardpulse_web.make_handler(
            runner=FakeRunner(),
            ui_path=ROOT_DIR / "web" / "index.html",
            allow_sms=False,
            base_path="/app/cardpulse",
            gateway_admin_only=True,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.addCleanup(cleanup)

        status, _, _ = self.raw_request(server, "GET", "/app/cardpulse")
        self.assertEqual(status, 403)

        status, headers, body = self.raw_request(
            server,
            "GET",
            "/app/cardpulse",
            headers={"X-Trim-Isadmin": "true"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(headers["Content-Security-Policy"], "frame-ancestors 'self'")
        self.assertIn('const CARDPULSE_BASE_PATH = "/app/cardpulse";', body)

    def test_gateway_post_requires_same_origin_and_gateway_csrf_token(self):
        handler = cardpulse_web.make_handler(
            runner=FakeRunner(),
            ui_path=None,
            allow_sms=False,
            base_path="/app/cardpulse",
            gateway_admin_only=True,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.addCleanup(cleanup)

        def request(method, path, payload=None, headers=None):
            conn = http.client.HTTPConnection(server.server_address[0], server.server_address[1], timeout=5)
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            request_headers = {"X-Trim-Isadmin": "true", **(headers or {})}
            if body is not None:
                request_headers.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            response_headers = dict(response.getheaders())
            data = json.loads(response.read().decode("utf-8"))
            conn.close()
            return response.status, response_headers, data

        status, headers, session = request("GET", "/app/cardpulse/api/auth/session")
        self.assertEqual(status, 200)
        self.assertTrue(session["authenticated"])
        self.assertTrue(session["csrf_token"])
        self.assertIn("Path=/app/cardpulse", headers["Set-Cookie"])
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        origin = "https://cardpulse.nas"
        gateway_origin_headers = {
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "cardpulse.nas",
        }

        status, _, rejected = request("POST", "/app/cardpulse/api/at", {"cmd": "AT"})
        self.assertEqual(status, 403)
        self.assertIn("CSRF", rejected["message"])

        status, _, rejected = request(
            "POST",
            "/app/cardpulse/api/at",
            {"cmd": "AT"},
            {
                "Origin": origin,
                "Cookie": cookie,
                "X-CardPulse-CSRF": "not-the-cookie-token",
            },
        )
        self.assertEqual(status, 403)
        self.assertIn("CSRF", rejected["message"])

        status, _, rejected = request(
            "POST",
            "/app/cardpulse/api/at",
            {"cmd": "AT"},
            {
                "Origin": "https://not-cardpulse.example",
                "Cookie": cookie,
                "X-CardPulse-CSRF": session["csrf_token"],
                **gateway_origin_headers,
            },
        )
        self.assertEqual(status, 403)
        self.assertIn("Origin", rejected["message"])

        status, _, accepted = request(
            "POST",
            "/app/cardpulse/api/at",
            {"cmd": "AT"},
            {
                "Origin": origin,
                "Cookie": cookie,
                "X-CardPulse-CSRF": session["csrf_token"],
                **gateway_origin_headers,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(accepted["ok"])

    def test_gateway_csrf_uses_forwarded_origin_only_for_gateway_requests(self):
        handler = cardpulse_web.make_handler(
            runner=FakeRunner(),
            ui_path=None,
            allow_sms=False,
            base_path="/app/cardpulse",
            gateway_admin_only=True,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.addCleanup(cleanup)

        def request(method, path, payload=None, headers=None):
            conn = http.client.HTTPConnection(server.server_address[0], server.server_address[1], timeout=5)
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            request_headers = {"X-Trim-Isadmin": "true", **(headers or {})}
            if body is not None:
                request_headers.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            response_headers = dict(response.getheaders())
            data = json.loads(response.read().decode("utf-8"))
            conn.close()
            return response.status, response_headers, data

        status, headers, session = request("GET", "/app/cardpulse/api/auth/session")
        self.assertEqual(status, 200)
        self.assertTrue(session["csrf_token"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]

        status, _, missing_forwarded = request(
            "POST",
            "/app/cardpulse/api/at",
            {"cmd": "AT"},
            {
                "Origin": f"http://{server.server_address[0]}:{server.server_address[1]}",
                "Cookie": cookie,
                "X-CardPulse-CSRF": session["csrf_token"],
            },
        )
        self.assertEqual(status, 403)
        self.assertIn("Origin", missing_forwarded["message"])

        status, _, insecure_forwarded = request(
            "POST",
            "/app/cardpulse/api/at",
            {"cmd": "AT"},
            {
                "Origin": "http://cardpulse.nas",
                "Cookie": cookie,
                "X-CardPulse-CSRF": session["csrf_token"],
                "X-Forwarded-Proto": "http",
                "X-Forwarded-Host": "cardpulse.nas",
            },
        )
        self.assertEqual(status, 403)
        self.assertIn("Origin", insecure_forwarded["message"])

        status, _, accepted = request(
            "POST",
            "/app/cardpulse/api/at",
            {"cmd": "AT"},
            {
                "Origin": "https://cardpulse.nas",
                "Cookie": cookie,
                "X-CardPulse-CSRF": session["csrf_token"],
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "cardpulse.nas",
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(accepted["ok"])

    def test_web_server_has_no_automatic_sms_cleanup_path(self):
        source = (ROOT_DIR / "lib" / "cardpulse_web.py").read_text(encoding="utf-8")

        self.assertNotIn("CleanupManager", source)
        self.assertNotIn("auto-cleanup-oldest-on-full", source)
        self.assertNotIn("CARDPULSE_WEB_AUTO_CLEANUP_OLDEST_ON_FULL", source)

    def test_main_records_sms_storage_startup_baseline_without_message_content(self):
        class ExitImmediatelyServer:
            def __init__(self, _address, handler):
                self.RequestHandlerClass = handler

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = MutableSmsStatusRunner()
            with (
                mock.patch.object(cardpulse_web, "CardPulseRunner", return_value=runner),
                mock.patch.object(cardpulse_web, "ThreadingHTTPServer", ExitImmediatelyServer),
                mock.patch.dict(os.environ, {"CARDPULSE_STATE_DIR": tmpdir}, clear=False),
            ):
                self.assertEqual(
                    cardpulse_web.main(["--root", str(ROOT_DIR), "--port", "0"]),
                    0,
                )

            audit_path = Path(tmpdir) / "sms-operations.jsonl"
            audit = json.loads(audit_path.read_text(encoding="utf-8").strip())
            self.assertEqual(audit["operation"], "startup_storage_baseline")
            self.assertEqual(audit["source"], "web")
            self.assertTrue(audit["ok"])
            self.assertEqual(audit["storage"]["name"], "ME")
            self.assertEqual(audit["storage"]["used"], 23)
            self.assertNotIn("body", audit)
            self.assertNotIn("preview", audit)

    def test_main_fnos_gateway_requires_unix_socket(self):
        with self.assertRaises(SystemExit) as context:
            cardpulse_web.main(["--fnos-gateway"])

        self.assertEqual(context.exception.code, 2)

    def test_main_fnos_gateway_requires_the_fixed_gateway_base_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(
                cardpulse_web,
                "ThreadingUnixHTTPServer",
                side_effect=AssertionError("server must not start"),
            ):
                with self.assertRaises(SystemExit) as context:
                    cardpulse_web.main(
                        [
                            "--fnos-gateway",
                            "--socket",
                            str(Path(tmpdir) / "app.sock"),
                            "--base-path",
                            "/wrong-path",
                        ]
                    )

        self.assertEqual(context.exception.code, 2)

    def test_main_removes_gateway_socket_after_graceful_shutdown(self):
        class ExitImmediatelyServer:
            def __init__(self, address, handler):
                self.RequestHandlerClass = handler
                Path(address).touch()

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            socket_path = root / "gateway" / "app.sock"
            runner = MutableSmsStatusRunner()
            with (
                mock.patch.object(cardpulse_web, "CardPulseRunner", return_value=runner),
                mock.patch.object(cardpulse_web, "ThreadingUnixHTTPServer", ExitImmediatelyServer),
                mock.patch.dict(
                    os.environ,
                    {
                        "CARDPULSE_CONFIG_DIR": str(root / "config"),
                        "CARDPULSE_STATE_DIR": str(root / "state"),
                    },
                    clear=False,
                ),
            ):
                self.assertEqual(
                    cardpulse_web.main(
                        [
                            "--root",
                            str(ROOT_DIR),
                            "--socket",
                            str(socket_path),
                            "--fnos-gateway",
                            "--base-path",
                            "/app/cardpulse",
                        ]
                    ),
                    0,
                )

            self.assertFalse(socket_path.exists())

    def test_main_corrects_config_state_and_auth_file_permissions(self):
        class ExitImmediatelyServer:
            def __init__(self, _address, handler):
                self.RequestHandlerClass = handler

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / "config"
            state_dir = root / "state"
            config_dir.mkdir()
            state_dir.mkdir()
            config_path = config_dir / "config.yaml"
            auth_path = config_dir / "web-auth.json"
            config_path.write_text("serial: {}\n", encoding="utf-8")
            auth_path.write_text("{}\n", encoding="utf-8")
            state_paths = (
                state_dir / "messages.jsonl",
                state_dir / "web-state.json",
                state_dir / "recovery.json",
                state_dir / "sms-operations.jsonl",
                state_dir / "local-history.jsonl",
                state_dir / "qdc507-readonly-acceptance.json",
                state_dir / "last_success",
                state_dir / "last_success_date",
                state_dir / "history.log",
                state_dir / "history.lock",
                state_dir / "cardpulse.lock",
            )
            for state_path in state_paths:
                state_path.write_text("{}\n", encoding="utf-8")
                state_path.chmod(0o644)
            runner = MutableSmsStatusRunner()

            with (
                mock.patch.object(cardpulse_web, "CardPulseRunner", return_value=runner),
                mock.patch.object(cardpulse_web, "ThreadingHTTPServer", ExitImmediatelyServer),
                mock.patch.object(
                    cardpulse_web,
                    "ensure_private_directory",
                    wraps=cardpulse_web.ensure_private_directory,
                ) as ensure_directory,
                mock.patch.object(
                    cardpulse_web,
                    "ensure_private_file",
                    wraps=cardpulse_web.ensure_private_file,
                ) as ensure_file,
                mock.patch.dict(
                    os.environ,
                    {
                        "CARDPULSE_CONFIG_DIR": str(config_dir),
                        "CARDPULSE_STATE_DIR": str(state_dir),
                    },
                    clear=False,
                ),
            ):
                self.assertEqual(
                    cardpulse_web.main(
                        ["--root", str(ROOT_DIR), "--port", "0", "--auth-file", str(auth_path)]
                    ),
                    0,
                )

            directory_paths = {Path(call.args[0]) for call in ensure_directory.call_args_list}
            file_paths = {Path(call.args[0]) for call in ensure_file.call_args_list}
            self.assertIn(config_dir, directory_paths)
            self.assertIn(state_dir, directory_paths)
            self.assertIn(config_path, file_paths)
            self.assertIn(auth_path, file_paths)
            for state_path in state_paths:
                self.assertIn(state_path, file_paths)
            if os.name != "nt":
                self.assertEqual(config_dir.stat().st_mode & 0o777, 0o700)
                self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)
                self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(auth_path.stat().st_mode & 0o777, 0o600)
                for state_path in state_paths:
                    self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)

    def test_fnos_gateway_refuses_to_start_when_private_permissions_cannot_be_hardened(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / "config"
            state_dir = root / "state"
            config_dir.mkdir()
            state_dir.mkdir()
            (config_dir / "config.yaml").write_text("serial: {}\n", encoding="utf-8")

            with (
                mock.patch.object(cardpulse_web.os, "chmod", side_effect=PermissionError("denied")),
                mock.patch.object(
                    cardpulse_web,
                    "ThreadingUnixHTTPServer",
                    side_effect=AssertionError("server must not start"),
                ),
                mock.patch.dict(
                    os.environ,
                    {
                        "CARDPULSE_CONFIG_DIR": str(config_dir),
                        "CARDPULSE_STATE_DIR": str(state_dir),
                    },
                    clear=False,
                ),
            ):
                with self.assertRaises(PermissionError):
                    cardpulse_web.main(
                        [
                            "--root",
                            str(ROOT_DIR),
                            "--socket",
                            str(root / "gateway" / "app.sock"),
                            "--fnos-gateway",
                            "--base-path",
                            "/app/cardpulse",
                        ]
                    )

    def test_web_ui_defaults_to_chinese(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('<html lang="zh-CN">', html)
        self.assertIn("设备概览", html)
        self.assertIn("推荐动作", html)
        self.assertIn("只读 AT 控制台", html)
        self.assertIn("测试短信", html)
        self.assertIn("短信测试默认关闭", html)
        self.assertIn("henry", html)

    def test_web_ui_has_loading_and_timeout_feedback(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("正在读取概览状态", html)
        self.assertIn("/api/overview", html)
        self.assertIn("正在运行硬件诊断", html)
        self.assertIn("请求超时", html)
        self.assertIn("function setMetric", html)
        self.assertIn("function refreshOverview", html)
        self.assertIn("data.raw || data.connection || data.sms_storage", html)

    def test_web_ui_refreshes_visible_views_without_overlapping_device_work(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("const AUTO_REFRESH_MS = 60000", html)
        self.assertIn("document.visibilityState !== \"visible\"", html)
        self.assertIn("liveRefreshTimer = window.setTimeout(async () => {", html)
        self.assertIn("await runVisibleAutoRefresh()", html)
        self.assertIn("scheduleLiveRefresh();", html)
        self.assertNotIn("setInterval(runVisibleAutoRefresh, AUTO_REFRESH_MS)", html)
        self.assertIn("let overviewRefreshInFlight = false", html)
        self.assertIn("let inboxRefreshInFlight = false", html)
        self.assertIn("async function refreshInbox", html)

    def test_web_ui_stops_live_refresh_when_the_session_ends(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("let sessionActive = false", html)
        self.assertIn("let liveRefreshTimer = null", html)
        self.assertIn("function stopLiveRefresh()", html)
        self.assertIn("window.clearTimeout(liveRefreshTimer)", html)
        self.assertIn("if (!sessionActive) return", html)
        self.assertNotIn("let autoRefreshStarted = false", html)

    def test_web_ui_has_structured_overview_and_sms_controls(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('data-view="inbox"', html)
        self.assertIn("/api/overview", html)
        self.assertIn("/api/messages/current", html)
        self.assertIn("/api/messages/history", html)
        self.assertIn("/api/messages/send", html)
        self.assertIn("/api/sms/status", html)
        self.assertIn("/api/sms/inbox", html)
        self.assertIn("/api/sms/delete", html)
        self.assertIn("消息中心", html)
        self.assertIn("当前模块", html)
        self.assertIn("本地历史", html)
        self.assertIn("发送短信", html)
        self.assertIn("方向筛选", html)
        self.assertIn("短信收件箱", html)
        self.assertIn("设备连接", html)
        self.assertIn("SIM / 网络", html)
        self.assertIn("短信容量", html)
        self.assertIn("保号任务", html)
        self.assertIn("最近消息", html)
        self.assertIn("恢复状态", html)
        self.assertIn("推荐动作", html)
        self.assertIn("读取可能会把未读短信标记为已读", html)
        self.assertIn("只删除明确无用的单条短信", html)
        self.assertIn("短信存储已满", html)
        self.assertIn("新短信提醒", html)
        self.assertIn("DELETE_SMS", html)
        self.assertIn("SEND_SMS", html)
        self.assertIn("function renderCurrentMessages", html)
        self.assertIn("function renderHistoryMessages", html)
        self.assertIn("function showMessageDetail", html)
        self.assertIn('data.ok && data.message && typeof data.message === "object"', html)
        self.assertIn("function applyOverview", html)
        self.assertIn("recovery-summary", html)
        self.assertIn("recovery-step", html)
        self.assertIn("recovery-hint", html)
        self.assertIn("last-inbound", html)
        self.assertIn("last-outbound", html)

    def test_web_ui_uses_native_dialogs_for_dangerous_sms_actions(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('<dialog id="sms-confirm-dialog">', html)
        self.assertIn('id="sms-confirm-token"', html)
        self.assertIn("function requestSmsConfirmation", html)
        self.assertIn("confirmDialog.showModal()", html)
        self.assertIn("requestSmsConfirmation({", html)
        self.assertNotIn("window.confirm(", html)
        self.assertNotIn("window.prompt(", html)

    def test_web_ui_has_login_gate_logout_and_csrf_request_support(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="login-screen"', html)
        self.assertIn('id="login-password"', html)
        self.assertIn('id="logout"', html)
        self.assertIn("/api/auth/session", html)
        self.assertIn("/api/auth/login", html)
        self.assertIn("/api/auth/logout", html)
        self.assertIn("/api/messages/history/clear", html)
        self.assertIn("CLEAR_LOCAL_HISTORY", html)
        self.assertIn("X-CardPulse-CSRF", html)
        self.assertIn("credentials: \"same-origin\"", html)
        self.assertIn("function showLogin", html)
        self.assertIn("function activateAuthenticatedUi", html)

    def test_web_ui_keeps_selected_message_highlighted(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn(".message-item.selected", html)
        self.assertIn("button.classList.toggle(\"selected\"", html)
        self.assertIn("selectedMessageKey", html)
        self.assertIn("function sortMessagesForDisplay", html)
        self.assertIn('id="delete-selected-sms"', html)
        self.assertIn("DELETE_SMS_BATCH", html)
        self.assertIn("selectedModuleIndexes", html)
        self.assertIn("is_multipart", html)
        self.assertIn("function scheduleLiveRefresh()", html)
        self.assertNotIn("setInterval(runVisibleAutoRefresh", html)

    def test_web_ui_has_guarded_force_delete_for_incomplete_multipart_sms(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="force-delete-incomplete-sms"', html)
        self.assertIn("FORCE_DELETE_INCOMPLETE_SMS", html)
        self.assertIn("selectedIncompleteMultipartIndexes", html)
        self.assertIn("/api/sms/delete-incomplete", html)

    def test_web_ui_prevents_selecting_more_than_five_sms_slots(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("const MAX_BATCH_DELETE_SLOTS = 5", html)
        self.assertIn("function canSelectModuleMessage", html)
        self.assertIn("批量删除最多选择", html)
        self.assertIn("return nextIndexes.size <= MAX_BATCH_DELETE_SLOTS", html)
        self.assertIn("const selectionLimitReached = !checkbox.checked && !canSelectModuleMessage(message)", html)
        self.assertIn("|| selectionLimitReached", html)

    def test_windows_recovery_script_verifies_doctor_and_keeps_sms_disabled_by_default(self):
        script = (ROOT_DIR / "scripts" / "start-dji-wsl-web.ps1").read_text(encoding="utf-8")

        self.assertIn("usbipd.exe @Arguments", script)
        self.assertIn("[1/7]", script)
        self.assertIn('@("list")', script)
        self.assertIn("2CA3:4006", script)
        self.assertIn("Resolve-DjiBusId", script)
        self.assertIn("Using DJI/Baiwang USB BusId", script)
        self.assertIn("skipping bind", script)
        self.assertIn('"bind", "--busid", $TargetBusId', script)
        self.assertIn('"attach", "--wsl", "--busid", $TargetBusId', script)
        self.assertIn("scripts/dji-qdc507-wsl-prepare.sh", script)
        self.assertIn("cardpulse --doctor", script)
        self.assertIn("AT: OK", script)
        self.assertIn("SIM: READY", script)
        self.assertIn("Network registration", script)
        self.assertIn("RSSI", script)
        self.assertIn("^[[:space:]]*RSSI: ([0-9]|[1-8][0-9]|9[0-8])[[:space:]]*$", script)
        self.assertIn("^[[:space:]]*Network registration: (1|5)[[:space:]]*$", script)
        self.assertIn("(?m)^\\s*RSSI: ([0-9]|[1-8][0-9]|9[0-8])\\s*$", script)
        self.assertIn("(?m)^\\s*Network registration: (1|5)\\s*$", script)
        self.assertNotIn('grep -Eq "RSSI: ([0-9]|[1-8][0-9]|9[0-8])"', script)
        self.assertNotIn('grep -Eq "Network registration: (1|5)"', script)
        self.assertIn("Detected AT serial port", script)
        self.assertIn("/api/health", script)
        self.assertIn("auth_required", script)
        self.assertNotIn('Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/info"', script)
        self.assertNotIn('Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/overview"', script)
        self.assertIn("recovery.json", script)
        self.assertIn("recovery.json.tmp", script)
        self.assertIn("base64 -d > $recoveryTempPathQ", script)
        self.assertIn("mv $recoveryTempPathQ $recoveryStatePathQ", script)
        self.assertIn("if ($LASTEXITCODE -ne 0)", script)
        self.assertNotIn("??", script)
        self.assertIn("function Expand-LiteralTemplate", script)
        self.assertIn("$detectScriptTemplate = @'", script)
        self.assertNotIn(r"\$(", script)
        self.assertIn("function Assert-CardPulseWebPort", script)
        self.assertIn("Get-NetTCPConnection", script)
        self.assertIn('-Step "web-port"', script)
        self.assertIn("[string]$Step", script)
        self.assertIn("[string]$PhaseStatus", script)
        self.assertIn("[string]$OperatorHint", script)
        self.assertIn("step = $StepSafe", script)
        self.assertIn("phase_status = $PhaseStatusSafe", script)
        self.assertIn("operator_hint = $OperatorHintSafe", script)
        self.assertIn("busid = $BusIdSafe", script)
        self.assertIn("distro = $DistroSafe", script)
        self.assertIn('$TargetBusId = ""', script)
        self.assertLess(
            script.index('try {\nSet-RecoveryStage -Step "web-port"'),
            script.index("$TargetBusId = Resolve-DjiBusId -OverrideBusId $BusId"),
        )
        self.assertIn("--host $HostBind --port $Port$allowSmsArg", script)
        self.assertIn("SMS test remains disabled", script)
        self.assertIn("~/.cardpulse-dji/config/config.yaml", (ROOT_DIR / "docs" / "web-control.md").read_text(encoding="utf-8"))
        self.assertNotIn("1024", script)
        self.assertNotIn("/tmp/cardpulse-dji-test", script)
        self.assertIn("if stripped == \"serial:\"", script)
        self.assertIn("line.startswith((\" \", \"\\t\")) and stripped.startswith(\"port:\")", script)

    def test_info_endpoint_adds_normalized_summary_fields(self):
        server, runner = self.start_server()

        status, data = self.request(server, "GET", "/api/info")

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(runner.commands, [["--info"]])
        self.assertEqual(data["device"], "/dev/ttyUSB2")
        self.assertEqual(data["sim"], "READY")
        self.assertEqual(data["signal"], "21")
        self.assertEqual(data["network"], "5")
        self.assertEqual(data["operator"], "CHINA MOBILE")
        self.assertEqual(data["imei"], "863212060375703")

    def test_ops_state_clears_only_matching_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = cardpulse_web.OpsState(Path(tmpdir) / "web-state.json")

            state.note_failure("overview failed", kind="overview")
            state.clear_failure("overview")
            self.assertIsNone(state.snapshot()["last_failure"])

            state.note_failure("send failed", kind="send")
            state.clear_failure("overview")
            self.assertEqual(state.snapshot()["last_failure"]["kind"], "send")

    def test_successful_overview_clears_matching_old_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RecoveringInfoRunner()
            server, _ = self.start_server(
                runner=runner,
                ops_state_path=Path(tmpdir) / "web-state.json",
            )

            first_status, first_data = self.request(server, "GET", "/api/overview")
            runner.fail_info = False
            second_status, second_data = self.request(server, "GET", "/api/overview")

            self.assertEqual(first_status, 200)
            self.assertIn("模组信息", first_data["recommended_action"])
            self.assertEqual(second_status, 200)
            self.assertNotIn("模组信息读取失败", second_data["recommended_action"])
            self.assertIsNone(second_data["last_failure"])

    def test_cardpulse_runner_serializes_device_commands(self):
        runner = cardpulse_web.CardPulseRunner(root_dir=ROOT_DIR)
        start = threading.Barrier(3)
        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def fake_run(*args, **kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with state_lock:
                active -= 1
            return subprocess.CompletedProcess(args[0], 0, "", "")

        def invoke(args):
            start.wait()
            return runner.run_cardpulse(args)

        with mock.patch.object(cardpulse_web.subprocess, "run", side_effect=fake_run):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(invoke, ["--info"]),
                    pool.submit(invoke, ["--sms-status"]),
                ]
                start.wait()
                for future in futures:
                    future.result()

        self.assertEqual(max_active, 1)

    def test_cardpulse_runner_serializes_mixed_device_commands(self):
        runner = cardpulse_web.CardPulseRunner(root_dir=ROOT_DIR)
        start = threading.Barrier(3)
        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def fake_run(*args, **kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with state_lock:
                active -= 1
            return subprocess.CompletedProcess(args[0], 0, "", "")

        def invoke_at():
            start.wait()
            runner.run_at("AT+CSQ", 5)

        def invoke_send():
            start.wait()
            runner.send_message("+15550000000", "lock test")

        with mock.patch.object(cardpulse_web.subprocess, "run", side_effect=fake_run):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(invoke_at)
                second = pool.submit(invoke_send)
                start.wait()
                first.result()
                second.result()

        self.assertEqual(max_active, 1)

    def test_status_endpoint_adds_schedule_summary_fields(self):
        server, runner = self.start_server()

        status, data = self.request(server, "GET", "/api/status")

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(runner.commands, [["--status"]])
        self.assertEqual(data["last_send"], "2026-07-09 11:02:03")
        self.assertEqual(data["days_since_last_send"], 0)
        self.assertEqual(data["interval_days"], 179)
        self.assertEqual(data["remaining_days"], 179)
        self.assertFalse(data["send_due"])
        self.assertEqual(data["next_send"], "2027-01-04 11:02:03")
        self.assertEqual(data["last_result"], "success")

    def test_sms_status_endpoint_adds_storage_summary_fields(self):
        server, runner = self.start_server()

        status, data = self.request(server, "GET", "/api/sms/status")

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(runner.commands, [["--sms-status"]])
        self.assertEqual(data["storage_name"], "ME")
        self.assertEqual(data["storage_used"], 23)
        self.assertEqual(data["storage_total"], 23)
        self.assertTrue(data["storage_full"])
        self.assertEqual(data["message_indication"], "2,1,0,0,0")

    def test_overview_endpoint_aggregates_health_and_recommendation(self):
        server, runner = self.start_server(allow_sms=False)

        status, data = self.request(server, "GET", "/api/overview")

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(runner.commands, [["--info"], ["--status"], ["--sms-status"]])
        self.assertEqual(data["overall_status"], "danger")
        self.assertIn("删除 1 条旧短信", data["recommended_action"])
        self.assertFalse(data["sms_enabled"])
        self.assertEqual(data["connection"]["port"], "/dev/ttyUSB2")
        self.assertEqual(data["sim"]["status"], "READY")
        self.assertEqual(data["signal"]["rssi"], 21)
        self.assertTrue(data["registration"]["registered"])
        self.assertEqual(data["sms_storage"]["remaining"], 0)
        self.assertEqual(data["sms_storage"]["severity"], "danger")
        self.assertEqual(data["keepalive"]["state"], "ok")
        self.assertEqual(data["keepalive"]["summary"], "保号正常，距离下次发送还有 179 天")
        self.assertIn("sms_status", data["raw"])
        self.assertIn("=== sms_status ===", data["output"])

    def test_message_center_detects_new_inbound_and_sends_summary_notification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RotatingInboxRunner()
            server, _ = self.start_server(
                runner=runner,
                history_path=Path(tmpdir) / "messages.jsonl",
                ops_state_path=Path(tmpdir) / "web-state.json",
            )

            first_status, first_data = self.request(server, "GET", "/api/messages/current")
            second_status, second_data = self.request(server, "GET", "/api/messages/current")

            self.assertEqual(first_status, 200)
            self.assertEqual(second_status, 200)
            self.assertEqual(first_data["new_message_count"], 0)
            self.assertEqual(second_data["new_message_count"], 1)
            self.assertEqual(len(second_data["new_messages"]), 1)
            self.assertEqual(second_data["new_messages"][0]["phone"], "+447700900123")
            self.assertEqual(len(runner.notifications), 1)
            self.assertIn("新短信提醒", runner.notifications[0][0])
            self.assertIn("Brand new inbound", runner.notifications[0][1])

    def test_overview_includes_recent_activity_recovery_and_pending_alerts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RotatingInboxRunner()
            history_path = Path(tmpdir) / "messages.jsonl"
            ops_state_path = Path(tmpdir) / "web-state.json"
            recovery_state_path = Path(tmpdir) / "recovery.json"
            recovery_state_path.write_text(json.dumps({
                "state": "ok",
                "step": "web",
                "phase_status": "complete",
                "summary": "WSL 恢复成功",
                "operator_hint": "",
                "checked_at": "2026-07-09T12:11:12+00:00",
                "port": "/dev/ttyUSB3",
                "web_url": "http://127.0.0.1:8765",
                "busid": "1-4",
                "distro": "Ubuntu-24.04",
            }, ensure_ascii=False), encoding="utf-8")
            server, _ = self.start_server(
                runner=runner,
                history_path=history_path,
                ops_state_path=ops_state_path,
                recovery_state_path=recovery_state_path,
            )

            self.request(server, "GET", "/api/messages/current")
            self.request(server, "GET", "/api/messages/current")
            status, data = self.request(server, "GET", "/api/overview")

            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(data["recent_messages"]["last_inbound"]["phone"], "+447700900123")
            self.assertEqual(data["recent_messages"]["last_outbound"], None)
            self.assertEqual(data["alerts"]["new_inbound"]["count"], 1)
            self.assertEqual(data["alerts"]["storage"]["level"], "ok")
            self.assertEqual(data["recovery"]["state"], "ok")
            self.assertEqual(data["recovery"]["step"], "web")
            self.assertEqual(data["recovery"]["phase_status"], "complete")
            self.assertEqual(data["recovery"]["summary"], "WSL 恢复成功")
            self.assertEqual(data["recovery"]["busid"], "1-4")
            self.assertEqual(data["recovery"]["distro"], "Ubuntu-24.04")
            self.assertIn("新短信", data["recommended_action"])

    def test_reading_pending_message_clears_new_inbound_alert(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RotatingInboxRunner()
            server, _ = self.start_server(
                runner=runner,
                history_path=Path(tmpdir) / "messages.jsonl",
                ops_state_path=Path(tmpdir) / "web-state.json",
            )

            self.request(server, "GET", "/api/messages/current")
            self.request(server, "GET", "/api/messages/current")
            before_status, before_data = self.request(server, "GET", "/api/overview")
            read_status, read_data = self.request(server, "GET", "/api/messages/current/2")
            after_status, after_data = self.request(server, "GET", "/api/overview")

            self.assertEqual(before_status, 200)
            self.assertEqual(read_status, 200)
            self.assertEqual(after_status, 200)
            self.assertEqual(before_data["alerts"]["new_inbound"]["count"], 1)
            self.assertTrue(read_data["ok"])
            self.assertEqual(after_data["alerts"]["new_inbound"]["count"], 0)

    def test_overview_defaults_recovery_to_neutral_when_missing(self):
        server, _ = self.start_server()

        status, data = self.request(server, "GET", "/api/overview")

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["recovery"]["state"], "")
        self.assertEqual(data["recovery"]["step"], "")
        self.assertEqual(data["recovery"]["phase_status"], "")
        self.assertEqual(data["recovery"]["summary"], "")
        self.assertEqual(data["recovery"]["operator_hint"], "")

    def test_load_recovery_state_handles_multiline_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "recovery.json"
            path.write_text(json.dumps({
                "state": "error",
                "step": "driver",
                "phase_status": "failed",
                "summary": "first line\nsecond line",
                "operator_hint": "Run sudo once in Ubuntu.",
                "checked_at": "2026-07-09T12:11:12+00:00",
                "port": "/dev/ttyUSB2",
                "web_url": "http://127.0.0.1:8765",
                "busid": "1-4",
                "distro": "Ubuntu-24.04",
            }, ensure_ascii=False), encoding="utf-8")

            data = cardpulse_web.load_recovery_state(path)

            self.assertEqual(data["state"], "error")
            self.assertEqual(data["step"], "driver")
            self.assertEqual(data["phase_status"], "failed")
            self.assertEqual(data["summary"], "first line\nsecond line")
            self.assertEqual(data["operator_hint"], "Run sudo once in Ubuntu.")
            self.assertEqual(data["busid"], "1-4")
            self.assertEqual(data["distro"], "Ubuntu-24.04")

    def test_load_recovery_state_ignores_broken_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "recovery.json"
            path.write_text("{not json", encoding="utf-8")

            data = cardpulse_web.load_recovery_state(path)

            self.assertEqual(data["state"], "")
            self.assertEqual(data["step"], "")
            self.assertEqual(data["phase_status"], "")
            self.assertEqual(data["summary"], "")
            self.assertEqual(data["operator_hint"], "")

    def test_recovery_failure_takes_priority_over_storage_and_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RotatingInboxRunner()
            recovery_state_path = Path(tmpdir) / "recovery.json"
            recovery_state_path.write_text(json.dumps({
                "state": "error",
                "step": "usbipd",
                "phase_status": "failed",
                "summary": "USB attach failed",
                "operator_hint": "Replug the module.",
                "checked_at": "2026-07-09T12:11:12+00:00",
                "port": "",
                "web_url": "",
                "busid": "1-4",
                "distro": "Ubuntu-24.04",
            }, ensure_ascii=False), encoding="utf-8")
            server, _ = self.start_server(
                runner=runner,
                history_path=Path(tmpdir) / "messages.jsonl",
                ops_state_path=Path(tmpdir) / "web-state.json",
                recovery_state_path=recovery_state_path,
            )

            self.request(server, "GET", "/api/messages/current")
            self.request(server, "GET", "/api/messages/current")
            status, data = self.request(server, "GET", "/api/overview")

            self.assertEqual(status, 200)
            self.assertEqual(data["recovery"]["state"], "error")
            self.assertEqual(data["recovery"]["step"], "usbipd")
            self.assertIn("USB attach failed", data["recommended_action"])

    def test_storage_alert_notifications_deduplicate_until_level_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = MutableSmsStatusRunner()
            server, _ = self.start_server(
                runner=runner,
                ops_state_path=Path(tmpdir) / "web-state.json",
            )

            first_status, first_data = self.request(server, "GET", "/api/overview")
            second_status, second_data = self.request(server, "GET", "/api/overview")
            runner.sms_status_output = (
                "Storage: ME 20/23\n"
                "Format: PDU\n"
                "New message indication: 2,1,0,0,0"
            )
            third_status, third_data = self.request(server, "GET", "/api/overview")

            self.assertEqual(first_status, 200)
            self.assertEqual(second_status, 200)
            self.assertEqual(third_status, 200)
            self.assertEqual(first_data["alerts"]["storage"]["level"], "danger")
            self.assertEqual(second_data["alerts"]["storage"]["level"], "danger")
            self.assertEqual(third_data["alerts"]["storage"]["level"], "ok")
            self.assertEqual(len(runner.notifications), 2)
            self.assertIn("短信容量告警", runner.notifications[0][0])
            self.assertIn("短信容量恢复", runner.notifications[1][0])

    def test_overview_endpoint_preserves_partial_details_when_sms_status_fails(self):
        failing_runner = FailingSmsStatusRunner()
        server, runner = self.start_server(allow_sms=False, runner=failing_runner)

        status, data = self.request(server, "GET", "/api/overview")

        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(runner.commands, [["--info"], ["--status"], ["--sms-status"]])
        self.assertEqual(data["connection"]["port"], "/dev/ttyUSB2")
        self.assertEqual(data["sim"]["status"], "READY")
        self.assertEqual(data["sms_storage"]["severity"], "danger")
        self.assertIn("短信存储读取失败", data["recommended_action"])
        self.assertIn("[ERROR] sms storage did not respond", data["raw"]["sms_status"])
        self.assertIn("=== sms_status ===", data["output"])

    def test_sms_test_requires_server_gate_and_confirmation(self):
        server, runner = self.start_server(allow_sms=False)

        status, data = self.request(server, "POST", "/api/actions/test-sms", {"confirm": "SEND_SMS"})

        self.assertEqual(status, 403)
        self.assertIn("disabled", data["message"])
        self.assertEqual(runner.commands, [])

        server, runner = self.start_server(allow_sms=True)
        status, data = self.request(server, "POST", "/api/actions/test-sms", {"confirm": "no"})

        self.assertEqual(status, 400)
        self.assertIn("confirmation", data["message"])
        self.assertEqual(runner.commands, [])

    def test_sms_test_runs_cardpulse_test_when_enabled_and_confirmed(self):
        server, runner = self.start_server(allow_sms=True)

        status, data = self.request(server, "POST", "/api/actions/test-sms", {"confirm": "SEND_SMS"})

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(runner.commands, [["--test"]])

    def test_sms_inbox_api_routes_to_cli_without_enabling_sms_send(self):
        server, runner = self.start_server(allow_sms=False)

        status, data = self.request(server, "GET", "/api/sms/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("FULL", data["output"])

        status, data = self.request(server, "GET", "/api/sms/inbox")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("Index: 1", data["output"])

        status, data = self.request(server, "GET", "/api/sms/messages/1")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("Message: OK", data["output"])

        self.assertEqual(runner.commands, [["--sms-status"], ["--inbox"], ["--read-sms", "1"]])

    def test_message_center_lists_current_module_messages_and_records_history(self):
        server, runner = self.start_server()

        status, data = self.request(server, "GET", "/api/messages/current")

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["source"], "module")
        self.assertEqual(len(data["messages"]), 2)
        self.assertEqual(data["messages"][0]["index"], "1")
        self.assertEqual(data["messages"][0]["direction"], "inbound")
        self.assertEqual(data["messages"][0]["from"], "+8613025523391")
        self.assertEqual(data["messages"][0]["time"], "2026-07-09 11:03:04")
        self.assertEqual(data["messages"][0]["preview"], "OK")
        self.assertEqual(data["messages"][0]["storage"], "module")

        history_status, history = self.request(server, "GET", "/api/messages/history")
        self.assertEqual(history_status, 200)
        self.assertTrue(history["ok"])
        self.assertEqual(len(history["messages"]), 2)
        message_two = next(message for message in history["messages"] if message["index"] == "2")
        self.assertEqual(message_two["body"], "Hello from phone")

    def test_message_center_preserves_multipart_indexes_and_blank_preview_lines(self):
        server, runner = self.start_server(runner=MultipartInboxRunner())

        status, data = self.request(server, "GET", "/api/messages/current")

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["messages"]), 4)
        multipart = next(message for message in data["messages"] if message["indexes"] == ["2", "3"])
        self.assertEqual(multipart["index"], "")
        self.assertTrue(multipart["is_multipart"])
        self.assertTrue(multipart["multipart_complete"])
        self.assertEqual(multipart["physical_slot_count"], 2)
        self.assertIn("Older multipart\n\nmessage body continues", multipart["body"])
        self.assertNotIn("stty failed", multipart["body"])
        self.assertFalse(any(not message["indexes"] for message in data["messages"]))
        self.assertEqual(runner.commands, [["--inbox"]])

    def test_multipart_acknowledgement_clears_pending_alert_without_modem_read(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = MultipartInboxRunner()
            server, _ = self.start_server(
                runner=runner,
                history_path=Path(tmpdir) / "messages.jsonl",
                ops_state_path=Path(tmpdir) / "web-state.json",
            )

            status, listed = self.request(server, "GET", "/api/messages/current")
            multipart = next(message for message in listed["messages"] if message["indexes"] == ["2", "3"])
            handler_state = cardpulse_web.OpsState(Path(tmpdir) / "web-state.json")
            handler_state.note_current_messages([multipart])

            acknowledge_status, acknowledged = self.request(
                server,
                "POST",
                "/api/messages/current/ack",
                {"id": multipart["id"], "indexes": multipart["indexes"]},
            )
            overview_status, overview = self.request(server, "GET", "/api/overview")

            self.assertEqual(status, 200)
            self.assertEqual(acknowledge_status, 200)
            self.assertTrue(acknowledged["ok"])
            self.assertEqual(overview_status, 200)
            self.assertEqual(overview["alerts"]["new_inbound"]["count"], 0)
            self.assertEqual(runner.commands.count(["--inbox"]), 1)

    def test_message_center_reads_one_current_message_with_full_body(self):
        server, runner = self.start_server()

        status, data = self.request(server, "GET", "/api/messages/current/2")

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["message"]["index"], "2")
        self.assertEqual(data["message"]["direction"], "inbound")
        self.assertEqual(data["message"]["body"], "Hello from phone")
        self.assertEqual(data["message"]["storage"], "module")
        self.assertEqual(runner.commands, [["--read-sms", "2"]])

    def test_message_center_does_not_fake_empty_message_when_read_unparseable(self):
        server, runner = self.start_server(runner=UnparseableReadSmsRunner())

        status, data = self.request(server, "GET", "/api/messages/current/9")

        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertNotIn("direction", data.get("message", {}))
        self.assertIn("no parseable message", data["message"])
        self.assertEqual(runner.commands, [["--read-sms", "9"]])

    def test_message_history_merges_module_preview_and_full_body(self):
        server, _ = self.start_server()

        list_status, listed = self.request(server, "GET", "/api/messages/current")
        read_status, read = self.request(server, "GET", "/api/messages/current/2")
        history_status, history = self.request(server, "GET", "/api/messages/history")

        self.assertEqual(list_status, 200)
        self.assertEqual(read_status, 200)
        self.assertEqual(history_status, 200)
        self.assertTrue(listed["ok"])
        self.assertTrue(read["ok"])
        matching = [
            item for item in history["messages"]
            if item["index"] == "2" and item["phone"] == "+447700900123"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["body"], "Hello from phone")

    def test_message_parser_preserves_multiline_sms_body(self):
        output = (
            "=== SMS message ===\n"
            "Index: 8\n"
            "Status: REC READ\n"
            "From: +447700900123\n"
            "Time: 2026-07-09 12:00:00\n"
            "Message: first line\n"
            "second line without a colon\n"
            "OTP: 123456\n"
            "From: Alice\n"
            "Time: 10:30\n"
            "Status: paid\n"
            "Preview: quoted text\n"
            "third line\n"
        )

        blocks = cardpulse_web.parse_message_blocks(output)
        message = cardpulse_web.normalize_sms_message(blocks[0], index="8")

        self.assertEqual(
            message["body"],
            "first line\n"
            "second line without a colon\n"
            "OTP: 123456\n"
            "From: Alice\n"
            "Time: 10:30\n"
            "Status: paid\n"
            "Preview: quoted text\n"
            "third line",
        )

    def test_message_parser_treats_indexes_as_new_records_and_blank_lines_as_body(self):
        output = (
            "=== SMS inbox ===\n"
            "Indexes: 20,22,21,19\n"
            "Status: REC READ\n"
            "From: +447700900123\n"
            "Time: 2026-07-07 14:10:44\n"
            "Parts: 4/4\n"
            "Preview: first paragraph\n"
            "\n"
            "second paragraph\n"
            "Index: 18\n"
            "Status: REC READ\n"
            "From: +8619957852909\n"
            "Time: 2026-07-09 04:01:04\n"
            "Preview: test 11:01\n"
        )

        blocks = cardpulse_web.parse_message_blocks(output)
        multipart = cardpulse_web.normalize_inbox_entry(blocks[0])

        self.assertEqual(len(blocks), 2)
        self.assertEqual(multipart["indexes"], ["20", "22", "21", "19"])
        self.assertEqual(multipart["physical_slot_count"], 4)
        self.assertTrue(multipart["multipart_complete"])
        self.assertEqual(multipart["body"], "first paragraph\n\nsecond paragraph")

    def test_message_history_is_newest_first_and_bounded(self):
        history = cardpulse_web.MessageHistory()
        history.add({"id": "old", "direction": "inbound", "time": "2026-07-09 10:00:00", "body": "old"})
        history.add({"id": "new", "direction": "inbound", "time": "2026-07-09 11:00:00", "body": "new"})

        self.assertEqual([item["id"] for item in history.list()], ["new", "old"])
        self.assertEqual([item["id"] for item in history.list(limit=1)], ["new"])

    def test_message_history_filters_by_direction(self):
        server, _ = self.start_server(allow_sms=True)

        send_status, sent = self.request(
            server,
            "POST",
            "/api/messages/send",
            {"phone": "+8613025523391", "message": "Outbound hello", "confirm": "SEND_SMS"},
        )
        self.assertEqual(send_status, 200)
        self.assertTrue(sent["ok"])

        self.request(server, "GET", "/api/messages/current/1")
        status, data = self.request(server, "GET", "/api/messages/history?direction=outbound")

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["messages"][0]["direction"], "outbound")
        self.assertEqual(data["messages"][0]["body"], "Outbound hello")

    def test_message_history_preserves_duplicate_outbound_sends(self):
        server, runner = self.start_server(allow_sms=True)

        for _ in range(2):
            status, data = self.request(
                server,
                "POST",
                "/api/messages/send",
                {"phone": "+8613025523391", "message": "Repeat hello", "confirm": "SEND_SMS"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])

        status, history = self.request(server, "GET", "/api/messages/history?direction=outbound")

        self.assertEqual(status, 200)
        self.assertEqual(len(history["messages"]), 2)
        self.assertEqual(runner.sent_messages, [
            ("+8613025523391", "Repeat hello"),
            ("+8613025523391", "Repeat hello"),
        ])

    def test_message_history_persists_to_local_jsonl_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = Path(tmpdir) / "messages.jsonl"
            server, _ = self.start_server(history_path=history_path)

            status, data = self.request(server, "GET", "/api/messages/current/2")
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])

            self.assertTrue(history_path.exists())
            lines = history_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            persisted = json.loads(lines[0])
            self.assertEqual(persisted["direction"], "inbound")
            self.assertEqual(persisted["body"], "Hello from phone")

            server.shutdown()
            server.server_close()
            server, _ = self.start_server(history_path=history_path)
            history_status, history = self.request(server, "GET", "/api/messages/history")

            self.assertEqual(history_status, 200)
            self.assertTrue(history["ok"])
            self.assertEqual(len(history["messages"]), 1)
            self.assertEqual(history["messages"][0]["body"], "Hello from phone")

    def test_message_history_persists_full_body_after_preview_upgrade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = Path(tmpdir) / "messages.jsonl"
            server, _ = self.start_server(history_path=history_path)

            list_status, listed = self.request(server, "GET", "/api/messages/current")
            read_status, read = self.request(server, "GET", "/api/messages/current/2")

            self.assertEqual(list_status, 200)
            self.assertEqual(read_status, 200)
            self.assertTrue(listed["ok"])
            self.assertTrue(read["ok"])

            server.shutdown()
            server.server_close()
            server, _ = self.start_server(history_path=history_path)
            history_status, history = self.request(server, "GET", "/api/messages/history")

            self.assertEqual(history_status, 200)
            matching = [
                item for item in history["messages"]
                if item["index"] == "2" and item["phone"] == "+447700900123"
            ]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]["body"], "Hello from phone")

    def test_message_send_requires_gate_confirmation_and_payload(self):
        server, runner = self.start_server(allow_sms=False)

        status, data = self.request(
            server,
            "POST",
            "/api/messages/send",
            {"phone": "+8613025523391", "message": "No send", "confirm": "SEND_SMS"},
        )
        self.assertEqual(status, 403)
        self.assertIn("disabled", data["message"])
        self.assertEqual(runner.sent_messages, [])

        server, runner = self.start_server(allow_sms=True)
        status, data = self.request(
            server,
            "POST",
            "/api/messages/send",
            {"phone": "+8613025523391", "message": "No send", "confirm": "WRONG"},
        )
        self.assertEqual(status, 400)
        self.assertIn("SEND_SMS", data["message"])
        self.assertEqual(runner.sent_messages, [])

        status, data = self.request(
            server,
            "POST",
            "/api/messages/send",
            {"phone": "", "message": "No send", "confirm": "SEND_SMS"},
        )
        self.assertEqual(status, 400)
        self.assertIn("phone", data["message"].lower())

        status, data = self.request(
            server,
            "POST",
            "/api/messages/send",
            {"phone": "not-a-phone", "message": "No send", "confirm": "SEND_SMS"},
        )
        self.assertEqual(status, 400)
        self.assertIn("phone", data["message"].lower())

        status, data = self.request(
            server,
            "POST",
            "/api/messages/send",
            {"phone": "+8613025523391", "message": "bad\nbody", "confirm": "SEND_SMS"},
        )
        self.assertEqual(status, 400)
        self.assertIn("control", data["message"].lower())

    def test_sms_delete_api_requires_single_index_and_confirmation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "sms-operations.jsonl"
            server, runner = self.start_server(operations_audit_path=audit_path)

            status, data = self.request(server, "GET", "/api/sms/messages/1-3")
            self.assertEqual(status, 400)
            self.assertIn("index", data["message"])

            status, data = self.request(server, "POST", "/api/sms/delete", {"index": "1"})
            self.assertEqual(status, 400)
            self.assertIn("confirmation", data["message"])

            status, data = self.request(server, "POST", "/api/sms/delete", {"index": "abc", "confirm": "DELETE_SMS"})
            self.assertEqual(status, 400)
            self.assertIn("index", data["message"])

            status, data = self.request(server, "POST", "/api/sms/delete", {"index": "1", "confirm": "DELETE_SMS"})
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertTrue(data["verified"])
            self.assertEqual(data["requested_indexes"], ["1"])
            self.assertEqual(data["command_succeeded_indexes"], ["1"])
            self.assertNotIn("1", data["remaining_indexes"])
            self.assertEqual(
                runner.commands,
                [
                    ["--delete-sms", "1", "--confirm", "DELETE_SMS"],
                    ["--inbox"],
                    ["--sms-status"],
                ],
            )

            audit = json.loads(audit_path.read_text(encoding="utf-8").strip())
            self.assertEqual(audit["operation"], "manual_single_delete")
            self.assertEqual(audit["source"], "web")
            self.assertTrue(audit["ok"])
            self.assertTrue(audit["verified"])
            self.assertEqual(audit["requested_count"], 1)
            self.assertEqual(audit["command_succeeded_count"], 1)
            self.assertNotIn("body", audit)
            self.assertNotIn("preview", audit)
            self.assertNotIn("requested_indexes", audit)
            self.assertNotIn("deleted_indexes", audit)

    def test_sms_delete_rejects_successful_command_when_slot_still_exists(self):
        server, runner = self.start_server(runner=StaleDeleteRunner())

        status, data = self.request(
            server,
            "POST",
            "/api/sms/delete",
            {"index": "2", "confirm": "DELETE_SMS"},
        )

        self.assertEqual(status, 409)
        self.assertFalse(data["ok"])
        self.assertFalse(data["verified"])
        self.assertEqual(data["requested_indexes"], ["2"])
        self.assertEqual(data["command_succeeded_indexes"], ["2"])
        self.assertIn("2", data["remaining_indexes"])
        self.assertEqual(
            runner.commands,
            [
                ["--delete-sms", "2", "--confirm", "DELETE_SMS"],
                ["--inbox"],
                ["--sms-status"],
            ],
        )

    def test_sms_delete_rejects_when_post_delete_storage_read_fails(self):
        server, runner = self.start_server(runner=VerificationSmsStatusFailureRunner())

        status, data = self.request(
            server,
            "POST",
            "/api/sms/delete",
            {"index": "2", "confirm": "DELETE_SMS"},
        )

        self.assertEqual(status, 502)
        self.assertFalse(data["ok"])
        self.assertFalse(data["verified"])
        self.assertEqual(data["command_succeeded_indexes"], ["2"])
        self.assertIn("storage", data["verification_error"])

    def test_sms_delete_rejects_unparseable_post_delete_inbox(self):
        server, runner = self.start_server(runner=VerificationMalformedInboxRunner())

        status, data = self.request(
            server,
            "POST",
            "/api/sms/delete",
            {"index": "2", "confirm": "DELETE_SMS"},
        )

        self.assertEqual(status, 409)
        self.assertFalse(data["ok"])
        self.assertFalse(data["verified"])
        self.assertIn("inbox", data["verification_error"].lower())

    def test_sms_delete_rejects_unparseable_post_delete_storage(self):
        server, runner = self.start_server(runner=VerificationMalformedSmsStatusRunner())

        status, data = self.request(
            server,
            "POST",
            "/api/sms/delete",
            {"index": "2", "confirm": "DELETE_SMS"},
        )

        self.assertEqual(status, 409)
        self.assertFalse(data["ok"])
        self.assertFalse(data["verified"])
        self.assertIn("storage", data["verification_error"].lower())

    def test_sms_batch_delete_rejects_partial_multipart_selection(self):
        server, runner = self.start_server(runner=MultipartInboxRunner())

        status, data = self.request(
            server,
            "POST",
            "/api/sms/delete-batch",
            {"indexes": ["2"], "confirm": "DELETE_SMS_BATCH"},
        )

        self.assertEqual(status, 409)
        self.assertFalse(data["ok"])
        self.assertIn("multipart", data["message"])
        self.assertEqual(runner.deleted_indexes, [])

    def test_sms_batch_delete_rejects_headerless_pre_delete_inbox(self):
        server, runner = self.start_server(runner=HeaderlessBatchInboxRunner())

        status, data = self.request(
            server,
            "POST",
            "/api/sms/delete-batch",
            {"indexes": ["2"], "confirm": "DELETE_SMS_BATCH"},
        )

        self.assertEqual(status, 409)
        self.assertFalse(data["ok"])
        self.assertIn("expected inbox contract", data["message"])
        self.assertNotIn(["--delete-sms", "2", "--confirm", "DELETE_SMS"], runner.commands)

    def test_sms_force_delete_incomplete_multipart_requires_special_confirmation_and_audits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "sms-operations.jsonl"
            server, runner = self.start_server(
                runner=IncompleteMultipartRunner(),
                operations_audit_path=audit_path,
            )

            status, data = self.request(
                server,
                "POST",
                "/api/sms/delete-incomplete",
                {"indexes": ["17"], "confirm": "DELETE_SMS_BATCH"},
            )
            self.assertEqual(status, 400)
            self.assertIn("FORCE_DELETE_INCOMPLETE_SMS", data["message"])
            self.assertEqual(runner.deleted_indexes, [])

            status, data = self.request(
                server,
                "POST",
                "/api/sms/delete-incomplete",
                {"indexes": ["17"], "confirm": "FORCE_DELETE_INCOMPLETE_SMS"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertTrue(data["verified"])
            self.assertEqual(data["deleted_indexes"], ["17"])
            self.assertEqual(
                runner.commands,
                [
                    ["--inbox"],
                    ["--delete-sms", "17", "--confirm", "DELETE_SMS"],
                    ["--inbox"],
                    ["--sms-status"],
                ],
            )

            audit = json.loads(audit_path.read_text(encoding="utf-8").strip())
            self.assertEqual(audit["operation"], "manual_incomplete_multipart_delete")
            self.assertTrue(audit["ok"])
            self.assertTrue(audit["verified"])
            self.assertEqual(audit["requested_count"], 1)
            self.assertEqual(audit["command_succeeded_count"], 1)
            self.assertNotIn("body", audit)

    def test_sms_force_delete_incomplete_rejects_headerless_pre_delete_inbox(self):
        server, runner = self.start_server(runner=HeaderlessIncompleteMultipartRunner())

        status, data = self.request(
            server,
            "POST",
            "/api/sms/delete-incomplete",
            {"indexes": ["17"], "confirm": "FORCE_DELETE_INCOMPLETE_SMS"},
        )

        self.assertEqual(status, 409)
        self.assertFalse(data["ok"])
        self.assertIn("expected inbox contract", data["message"])
        self.assertNotIn(["--delete-sms", "17", "--confirm", "DELETE_SMS"], runner.commands)

    def test_sms_force_delete_incomplete_rejects_complete_or_normal_sms(self):
        server, runner = self.start_server(runner=MultipartInboxRunner())

        status, data = self.request(
            server,
            "POST",
            "/api/sms/delete-incomplete",
            {"indexes": ["1", "2", "3"], "confirm": "FORCE_DELETE_INCOMPLETE_SMS"},
        )

        self.assertEqual(status, 409)
        self.assertFalse(data["ok"])
        self.assertIn("incomplete multipart", data["message"])
        self.assertEqual(runner.deleted_indexes, [])

    def test_sms_force_delete_incomplete_rejects_ambiguous_sms_slots(self):
        server, runner = self.start_server(runner=AmbiguousIncompleteMultipartRunner())

        status, data = self.request(
            server,
            "POST",
            "/api/sms/delete-incomplete",
            {"indexes": ["17"], "confirm": "FORCE_DELETE_INCOMPLETE_SMS"},
        )

        self.assertEqual(status, 409)
        self.assertFalse(data["ok"])
        self.assertIn("duplicated indexes", data["message"])
        self.assertEqual(runner.deleted_indexes, [])

    def test_sms_batch_delete_removes_selected_slots_in_descending_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "sms-operations.jsonl"
            server, runner = self.start_server(
                runner=MultipartInboxRunner(),
                operations_audit_path=audit_path,
            )

            status, data = self.request(
                server,
                "POST",
                "/api/sms/delete-batch",
                {"indexes": ["1", "2", "3"], "confirm": "DELETE_SMS_BATCH"},
            )

            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertTrue(data["verified"])
            self.assertEqual(data["deleted_indexes"], ["3", "2", "1"])
            self.assertEqual(runner.deleted_indexes, ["3", "2", "1"])

            audit = json.loads(audit_path.read_text(encoding="utf-8").strip())
            self.assertEqual(audit["operation"], "manual_batch_delete")
            self.assertTrue(audit["ok"])
            self.assertTrue(audit["verified"])
            self.assertEqual(audit["requested_count"], 3)
            self.assertEqual(audit["command_succeeded_count"], 3)
            self.assertNotIn("body", audit)

    def test_sms_batch_delete_stops_after_first_failure(self):
        server, runner = self.start_server(runner=FailingBatchDeleteRunner())

        status, data = self.request(
            server,
            "POST",
            "/api/sms/delete-batch",
            {"indexes": ["1", "2", "3", "4", "5", "6"], "confirm": "DELETE_SMS_BATCH"},
        )

        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertIn("at most 5", data["message"])
        self.assertEqual(runner.deleted_indexes, [])

    def test_sms_batch_delete_returns_partial_progress_after_delete_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "sms-operations.jsonl"
            server, runner = self.start_server(
                runner=FailingBatchDeleteRunner(),
                operations_audit_path=audit_path,
            )

            status, data = self.request(
                server,
                "POST",
                "/api/sms/delete-batch",
                {"indexes": ["4", "5", "6"], "confirm": "DELETE_SMS_BATCH"},
            )

            self.assertEqual(status, 500)
            self.assertFalse(data["ok"])
            self.assertEqual(data["deleted_indexes"], ["6"])
            self.assertEqual(data["failed_index"], "5")
            self.assertFalse(data["verified"])
            self.assertEqual(data["requested_indexes"], ["4", "5", "6"])
            self.assertEqual(data["command_succeeded_indexes"], ["6"])
            self.assertEqual(runner.deleted_indexes, ["6", "5"])

            audit = json.loads(audit_path.read_text(encoding="utf-8").strip())
            self.assertEqual(audit["operation"], "manual_batch_delete")
            self.assertFalse(audit["ok"])
            self.assertFalse(audit["verified"])
            self.assertEqual(audit["requested_count"], 3)
            self.assertEqual(audit["command_succeeded_count"], 1)

    def test_sms_force_delete_incomplete_returns_unverified_schema_after_partial_failure(self):
        server, runner = self.start_server(runner=FailingIncompleteMultipartDeleteRunner())

        status, data = self.request(
            server,
            "POST",
            "/api/sms/delete-incomplete",
            {"indexes": ["17", "18"], "confirm": "FORCE_DELETE_INCOMPLETE_SMS"},
        )

        self.assertEqual(status, 500)
        self.assertFalse(data["ok"])
        self.assertFalse(data["verified"])
        self.assertEqual(data["requested_indexes"], ["17", "18"])
        self.assertEqual(data["command_succeeded_indexes"], ["18"])
        self.assertEqual(data["deleted_indexes"], ["18"])
        self.assertEqual(data["failed_index"], "17")
        self.assertEqual(runner.deleted_indexes, ["18", "17"])

    def test_deleting_pending_message_clears_new_inbound_alert(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RotatingInboxRunner()
            server, _ = self.start_server(
                runner=runner,
                history_path=Path(tmpdir) / "messages.jsonl",
                ops_state_path=Path(tmpdir) / "web-state.json",
            )

            self.request(server, "GET", "/api/messages/current")
            self.request(server, "GET", "/api/messages/current")
            before_status, before_data = self.request(server, "GET", "/api/overview")
            delete_status, delete_data = self.request(
                server,
                "POST",
                "/api/sms/delete",
                {"index": "2", "confirm": "DELETE_SMS"},
            )
            after_status, after_data = self.request(server, "GET", "/api/overview")
            history_status, history = self.request(server, "GET", "/api/messages/history")

            self.assertEqual(before_status, 200)
            self.assertEqual(delete_status, 200)
            self.assertEqual(after_status, 200)
            self.assertEqual(history_status, 200)
            self.assertTrue(delete_data["ok"])
            self.assertTrue(delete_data["verified"])
            self.assertEqual(before_data["alerts"]["new_inbound"]["count"], 1)
            self.assertEqual(after_data["alerts"]["new_inbound"]["count"], 0)
            self.assertEqual(len(history["messages"]), 2)

    def test_failed_delete_keeps_pending_new_inbound_alert(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = FailingDeleteRunner()
            server, _ = self.start_server(
                runner=runner,
                history_path=Path(tmpdir) / "messages.jsonl",
                ops_state_path=Path(tmpdir) / "web-state.json",
            )

            self.request(server, "GET", "/api/messages/current")
            self.request(server, "GET", "/api/messages/current")
            before_status, before_data = self.request(server, "GET", "/api/overview")
            delete_status, delete_data = self.request(
                server,
                "POST",
                "/api/sms/delete",
                {"index": "2", "confirm": "DELETE_SMS"},
            )
            after_status, after_data = self.request(server, "GET", "/api/overview")

            self.assertEqual(before_status, 200)
            self.assertEqual(delete_status, 500)
            self.assertEqual(after_status, 200)
            self.assertFalse(delete_data["ok"])
            self.assertEqual(before_data["alerts"]["new_inbound"]["count"], 1)
            self.assertEqual(after_data["alerts"]["new_inbound"]["count"], 1)

    def test_manual_at_allows_only_readonly_commands_by_default(self):
        self.assertTrue(cardpulse_web.is_readonly_at_command("AT+CSQ"))
        self.assertTrue(cardpulse_web.is_readonly_at_command("AT+CPIN?"))
        self.assertTrue(cardpulse_web.is_readonly_at_command("ATI"))
        self.assertFalse(cardpulse_web.is_readonly_at_command("AT+CFUN=0"))
        self.assertFalse(cardpulse_web.is_readonly_at_command("AT+CMGS=1"))
        self.assertFalse(cardpulse_web.is_readonly_at_command("AT\r\nAT+CFUN=0"))

    def test_manual_at_endpoint_rejects_unsafe_command(self):
        server, runner = self.start_server()

        status, data = self.request(server, "POST", "/api/at", {"cmd": "AT+CFUN=0"})

        self.assertEqual(status, 400)
        self.assertIn("read-only", data["message"])
        self.assertEqual(runner.at_commands, [])

    def test_manual_at_endpoint_runs_safe_command(self):
        server, runner = self.start_server()

        status, data = self.request(server, "POST", "/api/at", {"cmd": "AT+CSQ", "timeout": 7})

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(runner.at_commands, [("AT+CSQ", 7)])


if __name__ == "__main__":
    os.chdir(ROOT_DIR)
    unittest.main()
