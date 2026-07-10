#!/usr/bin/env python3
import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT_DIR / "scripts" / "start-dji-wsl-web.ps1"


class WindowsRecoveryContractTest(unittest.TestCase):
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
