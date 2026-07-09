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
                "Storage: ME 23/23 FULL\n"
                "Format: PDU\n"
                "New message indication: 2,1,0,0,0",
                "",
            )
        if args == ["--inbox"]:
            return cardpulse_web.CommandResult(0, "Index: 1\nFrom: +8613025523391\nPreview: OK", "")
        if args == ["--read-sms", "1"]:
            return cardpulse_web.CommandResult(0, "Index: 1\nMessage: OK", "")
        if args == ["--delete-sms", "1", "--confirm", "DELETE_SMS"]:
            return cardpulse_web.CommandResult(0, "Deleted SMS index: 1", "")
        return cardpulse_web.CommandResult(0, "ran " + " ".join(args), "")

    def run_at(self, cmd, timeout):
        self.at_commands.append((cmd, timeout))
        return cardpulse_web.CommandResult(0, "OK", "")


class FailingSmsStatusRunner(FakeRunner):
    def run_cardpulse(self, args):
        if args == ["--sms-status"]:
            self.commands.append(list(args))
            return cardpulse_web.CommandResult(2, "", "[ERROR] sms storage did not respond")
        return super().run_cardpulse(args)


class WebAPITestCase(unittest.TestCase):
    def start_server(self, allow_sms=False, runner=None):
        runner = runner or FakeRunner()
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
        self.assertIn("推荐动作", html)
        self.assertIn("只读 AT 控制台", html)
        self.assertIn("测试短信", html)
        self.assertIn("短信测试默认关闭", html)

    def test_web_ui_has_loading_and_timeout_feedback(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("正在读取概览状态", html)
        self.assertIn("/api/overview", html)
        self.assertIn("正在运行硬件诊断", html)
        self.assertIn("请求超时", html)
        self.assertIn("function setMetric", html)
        self.assertIn("function refreshOverview", html)
        self.assertIn("data.raw || data.connection || data.sms_storage", html)

    def test_web_ui_has_structured_overview_and_sms_controls(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('data-view="inbox"', html)
        self.assertIn("/api/overview", html)
        self.assertIn("/api/sms/status", html)
        self.assertIn("/api/sms/inbox", html)
        self.assertIn("/api/sms/delete", html)
        self.assertIn("短信收件箱", html)
        self.assertIn("设备连接", html)
        self.assertIn("SIM / 网络", html)
        self.assertIn("短信容量", html)
        self.assertIn("保号任务", html)
        self.assertIn("推荐动作", html)
        self.assertIn("读取可能会把未读短信标记为已读", html)
        self.assertIn("只删除明确无用的单条短信", html)
        self.assertIn("短信存储已满", html)
        self.assertIn("DELETE_SMS", html)
        self.assertIn("function applyOverview", html)

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
        self.assertIn("/api/overview", script)
        self.assertIn("overview_status", script)
        self.assertIn("--host $HostBind --port $Port$allowSmsArg", script)
        self.assertIn("SMS test remains disabled", script)
        self.assertIn("~/.cardpulse-dji/config/config.yaml", (ROOT_DIR / "docs" / "web-control.md").read_text(encoding="utf-8"))
        self.assertNotIn("1024", script)
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

    def test_sms_delete_api_requires_single_index_and_confirmation(self):
        server, runner = self.start_server()

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
        self.assertEqual(runner.commands, [["--delete-sms", "1", "--confirm", "DELETE_SMS"]])

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
