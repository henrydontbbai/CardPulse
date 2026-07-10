#!/usr/bin/env python3
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "lib"))

import cardpulse_web  # noqa: E402


class FakeRunner:
    def __init__(self):
        self.commands = []
        self.at_commands = []
        self.sent_messages = []
        self.notifications = []

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
            return cardpulse_web.CommandResult(
                0,
                "=== SMS inbox ===\n"
                "Index: 1\n"
                "Status: REC READ\n"
                "From: +8613025523391\n"
                "Time: 2026-07-09 11:03:04\n"
                "Preview: OK\n\n"
                "Index: 2\n"
                "Status: REC UNREAD\n"
                "From: +447700900123\n"
                "Time: 2026-07-09 11:04:05\n"
                "Preview: Hello from phone\n",
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
        if args == ["--delete-sms", "1", "--confirm", "DELETE_SMS"]:
            return cardpulse_web.CommandResult(0, "Deleted SMS index: 1", "")
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
                output = (
                    "=== SMS inbox ===\n"
                    "Index: 1\n"
                    "Status: REC READ\n"
                    "From: +8613025523391\n"
                    "Time: 2026-07-09 11:03:04\n"
                    "Preview: Old message\n\n"
                    "Index: 2\n"
                    "Status: REC UNREAD\n"
                    "From: +447700900123\n"
                    "Time: 2026-07-09 12:10:11\n"
                    "Preview: Brand new inbound\n"
                )
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


class WebAPITestCase(unittest.TestCase):
    def start_server(self, allow_sms=False, runner=None, history_path=None, ops_state_path=None, recovery_state_path=None):
        runner = runner or FakeRunner()
        handler = cardpulse_web.make_handler(
            runner=runner,
            ui_path=None,
            allow_sms=allow_sms,
            history_path=history_path,
            ops_state_path=ops_state_path,
            recovery_state_path=recovery_state_path,
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

    def test_web_ui_refreshes_visible_views_without_overlapping_device_work(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("const AUTO_REFRESH_MS = 60000", html)
        self.assertIn("document.visibilityState !== \"visible\"", html)
        self.assertIn("setInterval(runVisibleAutoRefresh, AUTO_REFRESH_MS)", html)
        self.assertIn("let overviewRefreshInFlight = false", html)
        self.assertIn("let inboxRefreshInFlight = false", html)
        self.assertIn("async function refreshInbox", html)

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

    def test_web_ui_keeps_selected_message_highlighted(self):
        html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn(".message-item.selected", html)
        self.assertIn("button.classList.toggle(\"selected\"", html)
        self.assertIn("selectedMessageKey", html)
        self.assertIn("function sortMessagesForDisplay", html)

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
        self.assertEqual(history["messages"][1]["body"], "Hello from phone")

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
