#!/usr/bin/env python3
"""Contracts for the fnOS no-modem degradation path.

These tests are deliberately local.  They do not inspect or touch a real
serial device; hardware pass-through remains a 56 NAS acceptance task.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest


ROOT_DIR = Path(__file__).resolve().parents[1]
PACKAGE = ROOT_DIR / "packaging" / "fnos" / "cardpulse"
DOCKER_DIR = PACKAGE / "app" / "docker"
LIFECYCLE_DIR = PACKAGE / "cmd"
DEVICE_NODE = Path("/dev/cardpulse-at")


class FnosDeviceModeContractTest(unittest.TestCase):
    def _prepare_lifecycle_control(self, pkgvar: Path) -> Path:
        pkgvar.mkdir(parents=True)
        pkgvar.chmod(0o3770)
        lifecycle = pkgvar / "lifecycle"
        lifecycle.mkdir()
        lifecycle.chmod(0o2750)
        return lifecycle

    def _write_qdc507_sysfs_fixture(
        self,
        root: Path,
        *,
        vendor: str,
        product: str,
        duplicate_tty: bool = False,
    ) -> Path:
        sysfs_tty_root = root / "sys" / "class" / "tty"
        tty_dir = sysfs_tty_root / "null"
        tty_dir.mkdir(parents=True)
        null_stat = os.stat("/dev/null")
        (tty_dir / "dev").write_text(
            f"{os.major(null_stat.st_rdev)}:{os.minor(null_stat.st_rdev)}\n",
            encoding="utf-8",
        )

        usb_device = root / "sys" / "devices" / "usb" / "1-1"
        resolved_tty = usb_device / "1-1:1.0" / "tty" / "null"
        resolved_tty.mkdir(parents=True)
        (usb_device / "idVendor").write_text(f"{vendor}\n", encoding="utf-8")
        (usb_device / "idProduct").write_text(f"{product}\n", encoding="utf-8")
        (tty_dir / "device").symlink_to(resolved_tty, target_is_directory=True)

        if duplicate_tty:
            duplicate_dir = sysfs_tty_root / "shadow-tty"
            duplicate_dir.mkdir()
            (duplicate_dir / "dev").write_text(
                f"{os.major(null_stat.st_rdev)}:{os.minor(null_stat.st_rdev)}\n",
                encoding="utf-8",
            )

        return sysfs_tty_root

    def _run_enabled_mode_with_qdc507_fixture(self, *, vendor: str, product: str):
        helper = LIFECYCLE_DIR / "cardpulse-device-mode.sh"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            appdest = root / "target"
            pkgvar = root / "var"
            target_docker = appdest / "docker"
            shutil.copytree(DOCKER_DIR, target_docker)
            lifecycle = self._prepare_lifecycle_control(pkgvar)
            sysfs_tty_root = self._write_qdc507_sysfs_fixture(
                root,
                vendor=vendor,
                product=product,
            )

            log_file = root / "lifecycle.log"
            env = os.environ | {
                "TRIM_APPDEST": str(appdest),
                "TRIM_PKGVAR": str(pkgvar),
                "TRIM_TEMP_LOGFILE": str(log_file),
                "wizard_qdc507_device_mode": "enabled",
            }
            result = subprocess.run(
                [
                    "sh",
                    "-c",
                    f'. {shlex.quote(str(helper))}; '
                    "cardpulse_current_qdc507_identity() { "
                    "cardpulse_qdc507_identity_for_paths /dev/null "
                    f"{shlex.quote(str(sysfs_tty_root))}; "
                    "}; "
                    "configure_cardpulse_device_mode_from_wizard",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            return (
                result,
                (target_docker / "docker-compose.yaml").read_text(encoding="utf-8"),
                (lifecycle / "qdc507-device-mode.active").read_text(encoding="utf-8")
                if (lifecycle / "qdc507-device-mode.active").exists()
                else "",
                log_file.read_text(encoding="utf-8") if log_file.exists() else "",
            )

    def test_default_compose_is_device_free_and_only_explicit_template_maps_fixed_alias(self):
        default_compose = (DOCKER_DIR / "docker-compose.yaml").read_text(encoding="utf-8")
        no_device_compose = (DOCKER_DIR / "docker-compose.no-device.yaml").read_text(encoding="utf-8")
        device_compose = (DOCKER_DIR / "docker-compose.device.yaml").read_text(encoding="utf-8")

        self.assertNotIn("\n    devices:", default_compose)
        self.assertNotIn("\n    devices:", no_device_compose)
        self.assertIn("\n    devices:\n      - /dev/cardpulse-at:/dev/cardpulse-at", device_compose)

        for source in (default_compose, no_device_compose, device_compose):
            self.assertNotIn("privileged:", source)
            self.assertNotIn("/dev:/dev", source)
            self.assertNotIn("/dev/bus/usb", source)
            self.assertNotIn("ttyUSB", source)
            self.assertNotIn("ports:", source)

    def test_wizards_default_the_qdc507_device_mode_to_disabled(self):
        for filename in ("install", "config"):
            wizard = json.loads((PACKAGE / "wizard" / filename).read_text(encoding="utf-8"))
            items = [item for step in wizard for item in step["items"]]
            mode = next(item for item in items if item.get("field") == "wizard_qdc507_device_mode")

            self.assertEqual("select", mode["type"])
            self.assertEqual("disabled", mode["initValue"])
            self.assertEqual(
                {"disabled", "enabled"},
                {option["value"] for option in mode["options"]},
            )

    def test_lifecycle_selects_a_template_from_explicit_mode_and_fixed_character_device(self):
        helper = (LIFECYCLE_DIR / "cardpulse-device-mode.sh").read_text(encoding="utf-8")
        callbacks = "\n".join(
            (LIFECYCLE_DIR / name).read_text(encoding="utf-8")
            for name in ("install_callback", "config_callback", "upgrade_callback", "main")
        )

        self.assertIn('CARDPULSE_DEVICE_NODE="/dev/cardpulse-at"', helper)
        self.assertIn('CARDPULSE_SYSFS_TTY_ROOT="/sys/class/tty"', helper)
        self.assertIn('readonly CARDPULSE_DEVICE_NODE CARDPULSE_SYSFS_TTY_ROOT', helper)
        self.assertIn('cardpulse_use_system_path() {', helper)
        self.assertIn('PATH=/usr/sbin:/usr/bin:/sbin:/bin', helper)
        self.assertIn('export PATH', helper)
        self.assertRegex(
            helper,
            r"select_cardpulse_device_mode\(\) \{\n\s+cardpulse_use_system_path",
        )
        self.assertRegex(
            helper,
            r"configure_cardpulse_device_mode_from_wizard\(\) \{\n\s+cardpulse_use_system_path",
        )
        self.assertNotIn(
            "export PATH\n\nCARDPULSE_DEVICE_NODE",
            helper,
            "the gateway status path must keep its inherited PATH",
        )
        self.assertNotIn('CARDPULSE_SYSFS_TTY_ROOT:-', helper)
        self.assertRegex(
            helper,
            re.compile(
                r'(?:test[ \t]+-c[ \t]+"[$]device_node"|\[[ \t]+(?:![ \t]+)?-c[ \t]+"[$]device_node"[ \t]+\])'
            ),
        )
        self.assertIn("docker-compose.no-device.yaml", helper)
        self.assertIn("docker-compose.device.yaml", helper)
        self.assertIn('CONTROL_DIR="${TRIM_PKGVAR}/lifecycle"', helper)
        self.assertIn("qdc507-device-mode.requested", helper)
        self.assertIn("qdc507-device-mode.active", helper)
        self.assertIn("configure_cardpulse_device_mode_from_wizard", callbacks)
        self.assertIn("select_cardpulse_device_mode", callbacks)
        self.assertNotRegex(callbacks.lower(), re.compile(r"(?m)^(?!\\s*#)\\s*docker\\s+compose\\b"))
        self.assertNotIn("systemctl", callbacks.lower())

    @unittest.skipUnless(os.name == "posix", "requires a POSIX character device")
    def test_identity_helper_scans_every_tty_and_rejects_duplicate_device_numbers(self):
        helper = LIFECYCLE_DIR / "cardpulse-device-mode.sh"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sysfs_tty_root = self._write_qdc507_sysfs_fixture(
                root,
                vendor="2ca3",
                product="4006",
                duplicate_tty=True,
            )
            log_file = root / "lifecycle.log"
            result = subprocess.run(
                [
                    "sh",
                    "-c",
                    f'. {shlex.quote(str(helper))}; '
                    "cardpulse_qdc507_identity_for_paths /dev/null "
                    f"{shlex.quote(str(sysfs_tty_root))}",
                ],
                text=True,
                capture_output=True,
                env=os.environ | {"TRIM_TEMP_LOGFILE": str(log_file)},
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            log_output = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
            self.assertIn("multiple tty", log_output)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_legacy_state_symlink_is_never_followed_for_device_mode(self):
        helper = LIFECYCLE_DIR / "cardpulse-device-mode.sh"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            appdest = root / "target"
            pkgvar = root / "var"
            target_docker = appdest / "docker"
            shutil.copytree(DOCKER_DIR, target_docker)
            lifecycle = self._prepare_lifecycle_control(pkgvar)
            legacy_state = pkgvar / "state"
            legacy_state.mkdir()
            sentinel = root / "sentinel"
            sentinel.write_text("do-not-touch\n", encoding="utf-8")
            (legacy_state / "qdc507-device-mode.active").symlink_to(sentinel)

            result = subprocess.run(
                ["sh", "-c", f'. {shlex.quote(str(helper))}; configure_cardpulse_device_mode_from_wizard'],
                text=True,
                capture_output=True,
                env=os.environ
                | {
                    "TRIM_APPDEST": str(appdest),
                    "TRIM_PKGVAR": str(pkgvar),
                    "wizard_qdc507_device_mode": "disabled",
                },
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("do-not-touch\n", sentinel.read_text(encoding="utf-8"))
            self.assertEqual("degraded\n", (lifecycle / "qdc507-device-mode.active").read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_new_lifecycle_control_directory_is_usable_when_parent_is_setgid(self):
        helper = LIFECYCLE_DIR / "cardpulse-device-mode.sh"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            appdest = root / "target"
            pkgvar = root / "var"
            target_docker = appdest / "docker"
            shutil.copytree(DOCKER_DIR, target_docker)
            pkgvar.mkdir()
            pkgvar.chmod(0o3770)

            result = subprocess.run(
                ["sh", "-c", f'. {shlex.quote(str(helper))}; configure_cardpulse_device_mode_from_wizard'],
                text=True,
                capture_output=True,
                env=os.environ
                | {
                    "TRIM_APPDEST": str(appdest),
                    "TRIM_PKGVAR": str(pkgvar),
                    "wizard_qdc507_device_mode": "disabled",
                },
                check=False,
            )

            lifecycle = pkgvar / "lifecycle"
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(0o2750, lifecycle.stat().st_mode & 0o7777)
            self.assertEqual("degraded\n", (lifecycle / "qdc507-device-mode.active").read_text(encoding="utf-8"))
            self.assertEqual(0o640, (lifecycle / "qdc507-device-mode.active").stat().st_mode & 0o777)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_unsafe_lifecycle_active_symlink_fails_closed_before_device_overlay(self):
        helper = LIFECYCLE_DIR / "cardpulse-device-mode.sh"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            appdest = root / "target"
            pkgvar = root / "var"
            target_docker = appdest / "docker"
            shutil.copytree(DOCKER_DIR, target_docker)
            lifecycle = self._prepare_lifecycle_control(pkgvar)
            (lifecycle / "qdc507-device-mode.requested").write_text("enabled\n", encoding="utf-8")
            (lifecycle / "qdc507-device-mode.requested").chmod(0o600)
            sentinel = root / "sentinel"
            sentinel.write_text("do-not-touch\n", encoding="utf-8")
            active = lifecycle / "qdc507-device-mode.active"
            active.symlink_to(sentinel)
            log_file = root / "lifecycle.log"

            result = subprocess.run(
                [
                    "sh",
                    "-c",
                    f'. {shlex.quote(str(helper))}; '
                    "cardpulse_current_qdc507_identity() { return 0; }; "
                    "select_cardpulse_device_mode",
                ],
                text=True,
                capture_output=True,
                env=os.environ
                | {
                    "TRIM_APPDEST": str(appdest),
                    "TRIM_PKGVAR": str(pkgvar),
                    "TRIM_TEMP_LOGFILE": str(log_file),
                },
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                (DOCKER_DIR / "docker-compose.no-device.yaml").read_text(encoding="utf-8"),
                (target_docker / "docker-compose.yaml").read_text(encoding="utf-8"),
            )
            self.assertEqual("do-not-touch\n", sentinel.read_text(encoding="utf-8"))
            self.assertTrue(active.is_symlink())
            log_output = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
            self.assertIn("unsafe lifecycle control file", log_output)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_unsafe_lifecycle_active_fifo_is_not_read_or_repaired(self):
        helper = LIFECYCLE_DIR / "cardpulse-device-mode.sh"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            appdest = root / "target"
            pkgvar = root / "var"
            target_docker = appdest / "docker"
            shutil.copytree(DOCKER_DIR, target_docker)
            lifecycle = self._prepare_lifecycle_control(pkgvar)
            active = lifecycle / "qdc507-device-mode.active"
            os.mkfifo(active)
            log_file = root / "lifecycle.log"

            result = subprocess.run(
                ["sh", "-c", f'. {shlex.quote(str(helper))}; select_cardpulse_device_mode'],
                text=True,
                capture_output=True,
                timeout=3,
                env=os.environ
                | {
                    "TRIM_APPDEST": str(appdest),
                    "TRIM_PKGVAR": str(pkgvar),
                    "TRIM_TEMP_LOGFILE": str(log_file),
                },
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(active.exists())
            self.assertTrue(stat.S_ISFIFO(active.stat().st_mode))
            log_output = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
            self.assertIn("unsafe lifecycle control file", log_output)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_unsafe_lifecycle_active_mode_is_not_chmodded_or_used(self):
        helper = LIFECYCLE_DIR / "cardpulse-device-mode.sh"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            appdest = root / "target"
            pkgvar = root / "var"
            target_docker = appdest / "docker"
            shutil.copytree(DOCKER_DIR, target_docker)
            lifecycle = self._prepare_lifecycle_control(pkgvar)
            active = lifecycle / "qdc507-device-mode.active"
            active.write_text("enabled\n", encoding="utf-8")
            active.chmod(0o660)
            log_file = root / "lifecycle.log"

            result = subprocess.run(
                ["sh", "-c", f'. {shlex.quote(str(helper))}; select_cardpulse_device_mode'],
                text=True,
                capture_output=True,
                env=os.environ
                | {
                    "TRIM_APPDEST": str(appdest),
                    "TRIM_PKGVAR": str(pkgvar),
                    "TRIM_TEMP_LOGFILE": str(log_file),
                },
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(0o660, active.stat().st_mode & 0o777)
            self.assertEqual(
                (DOCKER_DIR / "docker-compose.no-device.yaml").read_text(encoding="utf-8"),
                (target_docker / "docker-compose.yaml").read_text(encoding="utf-8"),
            )
            log_output = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
            self.assertIn("unsafe lifecycle control file", log_output)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_no_device_template_activation_failure_aborts_lifecycle_before_platform_start(self):
        helper = LIFECYCLE_DIR / "cardpulse-device-mode.sh"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            appdest = root / "target"
            pkgvar = root / "var"
            target_docker = appdest / "docker"
            shutil.copytree(DOCKER_DIR, target_docker)
            self._prepare_lifecycle_control(pkgvar)
            prior_device_compose = (DOCKER_DIR / "docker-compose.device.yaml").read_text(encoding="utf-8")
            (target_docker / "docker-compose.yaml").write_text(prior_device_compose, encoding="utf-8")
            (target_docker / "docker-compose.no-device.yaml").unlink()
            log_file = root / "lifecycle.log"

            result = subprocess.run(
                ["sh", "-c", f'. {shlex.quote(str(helper))}; select_cardpulse_device_mode'],
                text=True,
                capture_output=True,
                env=os.environ
                | {
                    "TRIM_APPDEST": str(appdest),
                    "TRIM_PKGVAR": str(pkgvar),
                    "TRIM_TEMP_LOGFILE": str(log_file),
                },
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(prior_device_compose, (target_docker / "docker-compose.yaml").read_text(encoding="utf-8"))
            log_output = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
            self.assertIn("safe no-device", log_output)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_enabled_mode_selects_device_template_only_for_current_qdc507_identity(self):
        result, active_compose, active_mode, _ = self._run_enabled_mode_with_qdc507_fixture(
            vendor="2ca3",
            product="4006",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            (DOCKER_DIR / "docker-compose.device.yaml").read_text(encoding="utf-8"),
            active_compose,
        )
        self.assertEqual(
            "enabled\n",
            active_mode,
        )

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_enabled_mode_rejects_current_tty_with_wrong_usb_identity(self):
        result, active_compose, active_mode, log_output = self._run_enabled_mode_with_qdc507_fixture(
            vendor="2ca3",
            product="9999",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            (DOCKER_DIR / "docker-compose.no-device.yaml").read_text(encoding="utf-8"),
            active_compose,
        )
        self.assertEqual(
            "degraded\n",
            active_mode,
        )
        self.assertIn("2ca3:4006", log_output)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    @unittest.skipIf(DEVICE_NODE.exists(), "requires a host without the fixed QDC507 alias")
    def test_missing_fixed_alias_keeps_the_runtime_compose_in_degraded_mode(self):
        helper = LIFECYCLE_DIR / "cardpulse-device-mode.sh"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            appdest = root / "target"
            pkgvar = root / "var"
            target_docker = appdest / "docker"
            shutil.copytree(DOCKER_DIR, target_docker)
            lifecycle = self._prepare_lifecycle_control(pkgvar)
            log_file = root / "lifecycle.log"
            env = os.environ | {
                "TRIM_APPDEST": str(appdest),
                "TRIM_PKGVAR": str(pkgvar),
                "TRIM_TEMP_LOGFILE": str(log_file),
                "wizard_qdc507_device_mode": "enabled",
            }

            result = subprocess.run(
                ["sh", "-c", f'. "{helper}"; configure_cardpulse_device_mode_from_wizard'],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                (target_docker / "docker-compose.no-device.yaml").read_text(encoding="utf-8"),
                (target_docker / "docker-compose.yaml").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "degraded\n",
                (lifecycle / "qdc507-device-mode.active").read_text(encoding="utf-8"),
            )
            self.assertIn("not available", log_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
