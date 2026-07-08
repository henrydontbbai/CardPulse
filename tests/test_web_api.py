#!/usr/bin/env python3
import http.client
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "lib"))

import cardpulse_web  # noqa: E402


class FakeRunner:
    def __init__(self):
        self.commands = []
        self.at_commands = []

    def run_cardpulse(self, args):
        self.commands.append(list(args))
        if args == ["--info"]:
            return cardpulse_web.CommandResult(
                0,
                "\u8bbe\u5907: /dev/ttyUSB2\n"
                "\u6ce2\u7279\u7387: 115200\n"
                "\u5382\u5546: Baiwang\n"
                "\u578b\u53f7: QDC507\n"
                "IMEI: 863212060375703\n"
                "SIM \u5361: READY\n"
                "\u4fe1\u53f7\u5f3a\u5ea6: 21\n"
                "\u7f51\u7edc\u72b6\u6001: 5\n"
                "\u8fd0\u8425\u5546: CHINA MOBILE\n",
                "",
            )
        return cardpulse_web.CommandResult(0, "ran " + " ".join(args), "")

    def run_at(self, cmd, timeout):
        self.at_commands.append((cmd, timeout))
        return cardpulse_web.CommandResult(0, "OK", "")


class WebAPITestCase(unittest.TestCase):
    def start_server(self, allow_sms=False):
        runner = FakeRunner()
        handler = cardpulse_web.make_handler(
            runner=runner,
            ui_path=None,
            allow_sms=allow_sms,
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

    def test_health_reports_sms_gate(self):
        server, _ = self.start_server(allow_sms=False)

        status, data = self.request(server, "GET", "/api/health")

        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["sms_enabled"])

    def test_web_ui_defaults_to_chinese(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('<html lang="zh-CN">', html)
        self.assertIn("设备概览", html)
        self.assertIn("刷新信息", html)
        self.assertIn("只读 AT 控制台", html)
        self.assertIn("测试短信", html)
        self.assertIn("短信测试默认关闭", html)

    def test_web_ui_has_loading_and_timeout_feedback(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("正在读取模块信息", html)
        self.assertIn("正在运行硬件诊断", html)
        self.assertIn("请求超时", html)
        self.assertIn("function setMetric", html)

    def test_windows_recovery_script_keeps_sms_disabled_by_default(self):
        script = (ROOT_DIR / "scripts" / "start-dji-wsl-web.ps1").read_text(encoding="utf-8")

        self.assertIn("usbipd.exe attach --wsl --busid", script)
        self.assertIn("scripts/dji-qdc507-wsl-prepare.sh", script)
        self.assertIn("--host $HostBind --port $Port$allowSmsArg", script)
        self.assertIn("SMS test remains disabled", script)
        self.assertIn("~/.cardpulse-dji/config/config.yaml", (ROOT_DIR / "docs" / "web-control.md").read_text(encoding="utf-8"))
        self.assertNotIn("1024", script)

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
