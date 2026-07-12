#!/usr/bin/env python3
import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT_DIR / "scripts" / "start-dji-wsl-web.ps1"
WEB_PATH = ROOT_DIR / "web" / "index.html"
README_PATH = ROOT_DIR / "README.md"
WEB_CONTROL_DOC_PATH = ROOT_DIR / "docs" / "web-control.md"


class WindowsRecoveryContractTest(unittest.TestCase):
    def test_real_recovery_defaults_to_port_8766(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")
        docs = (ROOT_DIR / "docs" / "web-control.md").read_text(encoding="utf-8")

        self.assertIn('[int]$Port = 8766', script)
        self.assertIn("start-dji-wsl-web.ps1 -Port 8766", docs)

    def test_windows_recovery_script_has_no_automatic_sms_cleanup_switch(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertNotIn("AutoCleanupOldestOnFull", script)
        self.assertNotIn("auto-cleanup-oldest-on-full", script)

    def test_automatic_cleanup_is_absent_from_ui_and_documentation(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")
        web = WEB_PATH.read_text(encoding="utf-8")
        readme = README_PATH.read_text(encoding="utf-8")
        docs = WEB_CONTROL_DOC_PATH.read_text(encoding="utf-8")

        for content in (script, web, readme, docs):
            self.assertNotIn("AutoCleanupOldestOnFull", content)
            self.assertNotIn("auto-cleanup-oldest-on-full", content)

        self.assertNotIn("sms_cleanup", web)
        self.assertNotIn("cleanup-badge", web)
        self.assertNotIn("自动清理", web)
        self.assertNotIn("自动清理", readme)
        self.assertNotIn("Optional full-store cleanup", docs)
        self.assertNotIn("Automatic cleanup", docs)

    def test_wsl_recovery_defaults_to_loopback_and_is_diagnostic_only(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")
        readme = README_PATH.read_text(encoding="utf-8")
        docs = WEB_CONTROL_DOC_PATH.read_text(encoding="utf-8")

        self.assertIn('[string]$HostBind = "127.0.0.1"', script)
        self.assertIn("WSL 在当前阶段仅用于硬件诊断和恢复验证", readme)
        self.assertIn("WSL is diagnostic-only in this phase.", docs)
        self.assertNotIn("0.0.0.0", readme)
        self.assertNotIn("0.0.0.0", docs)

    def test_documentation_retains_explicit_manual_delete_guidance(self):
        readme = README_PATH.read_text(encoding="utf-8")
        docs = WEB_CONTROL_DOC_PATH.read_text(encoding="utf-8")

        self.assertIn("`DELETE_SMS`", readme)
        self.assertIn("`DELETE_SMS_BATCH`", readme)
        self.assertIn("`DELETE_SMS`", docs)
        self.assertIn("`DELETE_SMS_BATCH`", docs)

    def test_wsl_recovery_uses_anonymous_health_only_after_web_auth(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn('/api/health', script)
        self.assertNotIn('/api/info"', script)
        self.assertNotIn('/api/overview"', script)
        self.assertIn('auth_required = $health.auth_required', script)

    def test_bash_templates_are_literal_and_token_expanded(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("function Expand-LiteralTemplate", script)
        self.assertIn("$detectScriptTemplate = @'", script)
        self.assertIn("Expand-LiteralTemplate -Template $detectScriptTemplate", script)
        self.assertNotIn(r"\$(", script)
        self.assertNotIn(r'"\$candidates"', script)
        self.assertNotIn(r'"\$current_output"', script)
        self.assertIn('.Replace("`r`n", "`n")', script)
        self.assertIn("function Invoke-WslScriptCapture", script)
        self.assertIn("base64 -d | bash", script)
        self.assertIn("$doctorOutput = Invoke-WslScriptCapture -Script $detectScript", script)
        self.assertIn('path.write_text("\\n".join(lines) + "\\n", encoding="utf-8")', script)
        self.assertNotIn('path.write_text("\\\\n".join(lines) + "\\\\n", encoding="utf-8")', script)
        self.assertIn('actual_device=$(printf', script)
        self.assertIn('selected_device="${actual_device:-$device}"', script)

    def test_recovery_json_write_is_atomic_and_interpolation_safe(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("[Convert]::ToBase64String", script)
        self.assertIn("base64 -d > $recoveryTempPathQ", script)
        self.assertIn("mv $recoveryTempPathQ $recoveryStatePathQ", script)
        self.assertNotIn("tmp_path=$recoveryTempPathQ", script)
        self.assertNotIn("mv $tmp_path $recoveryStatePathQ", script)

    def test_script_checks_port_before_usb_attach(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("function Assert-CardPulseWebPort", script)
        self.assertIn("Get-NetTCPConnection", script)
        self.assertIn("Get-CimInstance Win32_Process", script)
        self.assertIn("Use -Port with another local port", script)
        self.assertIn('-Step "web-port"', script)
        self.assertLess(
            script.index("Assert-CardPulseWebPort -PortNumber $Port"),
            script.index("Invoke-UsbipdAttach -TargetBusId $TargetBusId"),
        )
        self.assertNotIn("pkill -f '[s]cripts/cardpulse-web.py'", script)

    def test_wsl_capture_tolerates_native_warning_stderr(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")
        start = script.index("function Invoke-WslCapture")
        end = script.index("function Assert-CardPulseWebPort")
        capture_function = script[start:end]

        self.assertIn("$previousErrorActionPreference = $ErrorActionPreference", capture_function)
        self.assertIn('$ErrorActionPreference = "Continue"', capture_function)
        self.assertIn("$ErrorActionPreference = $previousErrorActionPreference", capture_function)
        self.assertIn("if ($LASTEXITCODE -ne 0)", capture_function)

    def test_doctor_migrates_legacy_literal_newline_config(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn('if len(text.splitlines()) <= 1 and "\\\\n" in text:', script)
        self.assertIn(
            'text = text.replace("\\\\r\\\\n", "\\n").replace("\\\\n", "\\n")',
            script,
        )

    def test_driver_uses_interactive_sudo_without_password_parameters(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("function Invoke-WslInteractive", script)
        self.assertIn('sudo bash scripts/dji-qdc507-wsl-prepare.sh', script)
        self.assertNotIn("[string]$SudoPassword", script)
        self.assertNotIn("CARDPULSE_WSL_SUDO_PASSWORD", script)
        self.assertNotIn("sudo -S bash scripts/dji-qdc507-wsl-prepare.sh", script)

    def test_windows_powershell_parser_accepts_script_when_available(self):
        if os.name != "nt":
            self.skipTest("Windows PowerShell parser test runs only on native Windows")

        powershell = shutil.which("powershell.exe")
        if not powershell:
            self.skipTest("Windows PowerShell is not available on this host")

        quoted_path = str(SCRIPT_PATH).replace("'", "''")
        command = (
            "$tokens=$null; $errors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            f"'{quoted_path}', [ref]$tokens, [ref]$errors) > $null; "
            "if ($errors.Count -gt 0) { "
            "$errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
