#!/usr/bin/env python3
"""Static contracts for the NAS deployment assets.

These tests intentionally inspect files only. They must remain safe to run on
developer machines without a NAS, systemd, Caddy, or attached modem.
"""

from __future__ import annotations

import re
import json
import os
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
DEPLOY_DIR = ROOT_DIR / "deploy" / "nas"
SCRIPT_DIR = ROOT_DIR / "scripts"
README_PATH = ROOT_DIR / "README.md"
NAS_DOC_PATH = ROOT_DIR / "docs" / "nas-deployment.md"
WEB_CONTROL_DOC_PATH = ROOT_DIR / "docs" / "web-control.md"

UNIT_NAMES = (
    "cardpulse-modem-ready.service",
    "cardpulse.service",
    "cardpulse.timer",
    "cardpulse-web.service",
)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_migration_module():
    path = SCRIPT_DIR / "nas-migrate-state.py"
    spec = importlib.util.spec_from_file_location("nas_migrate_state_test", path)
    if not spec or not spec.loader:
        raise RuntimeError("unable to load NAS migration module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NasDeploymentContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.units = {
            unit_name: read_text(DEPLOY_DIR / unit_name) for unit_name in UNIT_NAMES
        }
        self.modem_unit = self.units["cardpulse-modem-ready.service"]
        self.cardpulse_unit = self.units["cardpulse.service"]
        self.timer_unit = self.units["cardpulse.timer"]
        self.web_unit = self.units["cardpulse-web.service"]
        self.caddyfile = read_text(DEPLOY_DIR / "Caddyfile")
        self.udev_rule = read_text(DEPLOY_DIR / "99-cardpulse-qdc507.rules")
        self.preflight = read_text(SCRIPT_DIR / "nas-preflight.sh")
        self.modem_prepare = read_text(SCRIPT_DIR / "nas-modem-prepare.sh")
        self.installer = read_text(SCRIPT_DIR / "nas-install.sh")
        self.verifier = read_text(SCRIPT_DIR / "nas-verify.sh")

    def test_all_required_static_assets_exist(self) -> None:
        expected_paths = (
            DEPLOY_DIR / "99-cardpulse-qdc507.rules",
            DEPLOY_DIR / "Caddyfile",
            *(DEPLOY_DIR / unit_name for unit_name in UNIT_NAMES),
            SCRIPT_DIR / "nas-preflight.sh",
            SCRIPT_DIR / "nas-modem-prepare.sh",
            SCRIPT_DIR / "nas-install.sh",
            SCRIPT_DIR / "nas-migrate-state.py",
            SCRIPT_DIR / "nas-verify.sh",
        )
        self.assertTrue(
            all(path.is_file() for path in expected_paths),
            "missing NAS deployment asset",
        )

    def test_udev_rule_matches_qdc507_and_starts_preparation(self) -> None:
        self.assertIn('ATTRS{idVendor}=="2ca3"', self.udev_rule)
        self.assertIn('ATTRS{idProduct}=="4006"', self.udev_rule)
        self.assertIn('GROUP="cardpulse"', self.udev_rule)
        self.assertIn('MODE="0660"', self.udev_rule)
        self.assertIn('TAG+="systemd"', self.udev_rule)
        self.assertIn(
            'ENV{SYSTEMD_WANTS}+="cardpulse-modem-ready.service"', self.udev_rule
        )

    def test_only_modem_preparation_runs_as_root(self) -> None:
        self.assertRegex(self.modem_unit, r"(?m)^User=root$")
        self.assertRegex(self.cardpulse_unit, r"(?m)^User=cardpulse$")
        self.assertRegex(self.web_unit, r"(?m)^User=cardpulse$")
        self.assertNotRegex(self.cardpulse_unit, r"(?m)^User=root$")
        self.assertNotRegex(self.web_unit, r"(?m)^User=root$")

    def test_modem_preparation_is_root_only_and_non_destructive(self) -> None:
        self.assertRegex(self.modem_unit, r"(?m)^Type=oneshot$")
        self.assertNotIn("RemainAfterExit=yes", self.modem_unit)
        self.assertIn("nas-modem-prepare.sh", self.modem_unit)
        self.assertIn("modprobe option", self.modem_prepare)
        self.assertIn("new_id", self.modem_prepare)
        self.assertIn("/dev/ttyUSB", self.modem_prepare)
        self.assertIn("AT", self.modem_prepare)
        self.assertIn("SIM READY", self.modem_prepare)
        self.assertIn("CREG", self.modem_prepare)
        self.assertIn("/dev/cardpulse-at", self.modem_prepare)
        self.assertNotIn("chmod a+rw", self.modem_prepare)
        self.assertNotIn("--delete-sms", self.modem_prepare)
        self.assertNotIn("--allow-sms", self.modem_prepare)

    def test_application_uses_the_stable_alias_without_auto_detection(self) -> None:
        self.assertRegex(
            self.installer,
            r"port:\s*[\"']?/dev/cardpulse-at[\"']?",
        )
        self.assertRegex(self.installer, r"auto_detect:\s*false")
        self.assertIn("CARDPULSE_CONFIG_DIR=/var/lib/cardpulse/config", self.cardpulse_unit)
        self.assertIn("CARDPULSE_STATE_DIR=/var/lib/cardpulse/state", self.cardpulse_unit)
        self.assertIn("Requires=cardpulse-modem-ready.service", self.cardpulse_unit)

    def test_web_service_is_loopback_only_and_does_not_require_the_modem(self) -> None:
        self.assertRegex(self.web_unit, r"--host\s+127\.0\.0\.1")
        self.assertRegex(self.web_unit, r"--port\s+8766")
        self.assertIn("CARDPULSE_SERVICE_MODE=nas", self.web_unit)
        self.assertNotIn("Requires=cardpulse-modem-ready.service", self.web_unit)
        self.assertNotIn("After=cardpulse-modem-ready.service", self.web_unit)

    def test_caddy_is_the_public_tls_endpoint(self) -> None:
        self.assertIn("__CARDPULSE_NAS_HOSTNAME__", self.caddyfile)
        self.assertIn("tls internal", self.caddyfile)
        self.assertIn("reverse_proxy 127.0.0.1:8766", self.caddyfile)
        self.assertIn("header_up X-Forwarded-Proto {scheme}", self.caddyfile)
        self.assertIn("header_up X-Forwarded-For {remote_host}", self.caddyfile)
        self.assertNotIn(":8766", self.caddyfile.split("reverse_proxy", 1)[0])

    def test_cardpulse_timer_is_the_only_persistent_scheduler(self) -> None:
        timer_units = [
            unit_name
            for unit_name, content in self.units.items()
            if re.search(r"(?m)^\[Timer\]$", content)
        ]
        self.assertEqual(timer_units, ["cardpulse.timer"])
        self.assertIn("OnCalendar=", self.timer_unit)
        self.assertIn("Persistent=true", self.timer_unit)
        self.assertRegex(self.timer_unit, r"(?m)^RandomizedDelaySec=\S+$")

    def test_installer_creates_private_cardpulse_state_without_a_cron_schedule(self) -> None:
        self.assertIn("cardpulse", self.installer)
        self.assertIn("/var/lib/cardpulse", self.installer)
        self.assertIn("0700", self.installer)
        self.assertIn("0600", self.installer)
        self.assertIn("CARDPULSE_WEB_PUBLIC_ORIGIN", self.installer)
        self.assertIn('install -m 0755 "$INSTALL_ROOT/bin/cardpulse" /usr/local/bin/cardpulse', self.installer)
        self.assertIn("render_caddyfile", self.installer)
        self.assertIn("invalid HTTPS public origin", self.installer)
        self.assertIn("python3 -c 'import yaml'", self.installer)
        self.assertIn(
            "systemctl enable cardpulse-web.service caddy.service",
            self.installer,
        )
        self.assertIn("systemctl restart cardpulse-web.service", self.installer)
        self.assertIn("systemctl reload-or-restart caddy.service", self.installer)
        self.assertIn("udevadm control --reload-rules", self.installer)
        self.assertIn("systemctl disable --now cardpulse.timer", self.installer)
        self.assertLess(
            self.installer.index("systemctl daemon-reload"),
            self.installer.index("systemctl disable --now cardpulse.timer"),
        )
        self.assertNotRegex(self.installer, r"(?m)^\s*\S.*\*\s+\*\s+\*\s+\*\s+\*")
        self.assertNotIn("systemctl enable cardpulse.timer", self.installer)
        self.assertNotIn("systemctl enable --now cardpulse.timer", self.installer)

    def test_installer_requires_a_valid_web_password_before_exposing_services(self) -> None:
        self.assertIn("ensure_web_auth_config", self.installer)
        self.assertIn("cardpulse-web-password.py", self.installer)
        self.assertIn("password_config_is_valid", self.installer)
        self.assertGreater(self.installer.count("ensure_web_auth_config"), 1)
        auth_gate = self.installer.rindex("ensure_web_auth_config")
        self.assertLess(auth_gate, self.installer.index("systemctl enable cardpulse-web.service caddy.service"))
        self.assertLess(auth_gate, self.installer.index("systemctl restart cardpulse-web.service"))

    def test_installer_creates_service_identity_before_private_directories(self) -> None:
        self.assertIn("groupadd --system cardpulse", self.installer)
        self.assertIn("useradd --system --gid cardpulse", self.installer)
        self.assertLess(
            self.installer.index("useradd --system --gid cardpulse"),
            self.installer.index('install -d -o cardpulse -g cardpulse -m 0700'),
        )
        self.assertIn('install -d -m 0755 "$INSTALL_ROOT/bin" "$INSTALL_ROOT/lib" "$INSTALL_ROOT/scripts"', self.installer)
        self.assertIn('install -m 0755 "$ROOT_DIR/bin/cardpulse" "$INSTALL_ROOT/bin/cardpulse"', self.installer)
        self.assertIn('install -m 0644 "$ROOT_DIR/lib/"*.sh "$ROOT_DIR/lib/"*.py "$INSTALL_ROOT/lib/"', self.installer)
        self.assertIn('install -m 0755 "$ROOT_DIR/scripts/cardpulse-web.py" "$INSTALL_ROOT/scripts/cardpulse-web.py"', self.installer)
        self.assertIn('install -m 0755 "$ROOT_DIR/scripts/cardpulse-web-password.py" "$INSTALL_ROOT/scripts/cardpulse-web-password.py"', self.installer)
        self.assertIn('install -m 0755 "$ROOT_DIR/scripts/nas-origin.py" "$INSTALL_ROOT/scripts/nas-origin.py"', self.installer)
        self.assertIn('"$INSTALL_ROOT/web"', self.installer)
        self.assertIn(
            'install -m 0644 "$ROOT_DIR/web/index.html" "$INSTALL_ROOT/web/index.html"',
            self.installer,
        )
        self.assertIn('/usr/local/lib/cardpulse', self.installer)
        self.assertIn(
            'install -m 0755 "$SCRIPT_DIR/nas-modem-prepare.sh" /usr/local/lib/cardpulse/nas-modem-prepare.sh',
            self.installer,
        )
        self.assertNotIn('cp -a "$ROOT_DIR/." "$INSTALL_ROOT/"', self.installer)

    def test_preflight_and_verify_are_non_destructive(self) -> None:
        for command in (
            "systemctl",
            "python3",
            "flock",
            "caddy",
            "modinfo option",
            "lsusb -d 2ca3:4006",
            "crontab",
        ):
            self.assertIn(command, self.preflight)

        for command in (
            "cardpulse --doctor",
            "cardpulse --info",
            "cardpulse --sms-status",
            "cardpulse --status",
            "caddy",
        ):
            self.assertIn(command, self.verifier)

        for source in (self.preflight, self.verifier):
            self.assertNotIn("--delete-sms", source)
            self.assertNotIn("--allow-sms", source)
            self.assertNotIn("systemctl start", source)

    def test_preflight_and_verify_fail_closed_for_production_prerequisites(self) -> None:
        self.assertIn('[[ "${EUID}" -eq 0 ]]', self.preflight)
        self.assertIn("python3 -c 'import yaml'", self.preflight)
        self.assertIn("required command is missing", self.preflight)
        self.assertIn("port 8766 is already listening", self.preflight)

        self.assertIn('[[ "${EUID}" -eq 0 ]]', self.verifier)
        self.assertNotIn("|| true", self.verifier)
        self.assertIn("systemctl is-active --quiet cardpulse-web.service", self.verifier)
        self.assertIn("systemctl is-active --quiet caddy.service", self.verifier)
        self.assertIn("systemctl list-unit-files 'cardpulse*.timer'", self.verifier)
        self.assertIn("CardPulse timer must remain disabled", self.verifier)
        self.assertIn('[[ -L "$AT_ALIAS" ]]', self.verifier)
        self.assertIn("127.0.0.1:8766", self.verifier)
        self.assertIn("web-auth.json", self.verifier)

    def test_cron_is_checked_when_present_but_not_required_on_a_timer_only_nas(self) -> None:
        self.assertIn("command -v crontab", self.preflight)
        self.assertIn("command -v crontab", self.installer)
        self.assertIn("command -v crontab", self.verifier)
        self.assertNotIn("udevadm crontab", self.preflight)
        self.assertNotIn("useradd crontab", self.installer)
        self.assertNotIn("runuser crontab", self.verifier)

    def test_installer_fails_if_a_legacy_cardpulse_cron_cannot_be_removed(self) -> None:
        self.assertIn("remove_legacy_cardpulse_cron", self.installer)
        self.assertIn("legacy CardPulse cron entry remains after removal", self.installer)
        self.assertNotIn("grep -vi cardpulse | crontab - || true", self.installer)

    def test_installer_removes_legacy_cardpulse_cron_from_every_supported_location(self) -> None:
        self.assertIn("crontab -u cardpulse", self.installer)
        self.assertIn("/etc/crontab", self.installer)
        self.assertIn("/etc/cron.d", self.installer)
        self.assertIn("remove_cardpulse_cron_file", self.installer)

    def test_deployment_assets_have_no_automatic_sms_cleanup(self) -> None:
        sources = (
            *self.units.values(),
            self.caddyfile,
            self.udev_rule,
            self.preflight,
            self.modem_prepare,
            self.installer,
            self.verifier,
        )
        for source in sources:
            self.assertNotIn("auto-cleanup", source.casefold())
            self.assertNotIn("cleanup-oldest", source.casefold())
            self.assertNotIn("--delete-sms", source)

    def test_migration_is_explicit_safe_and_rewrites_stable_serial_path(self) -> None:
        migration = read_text(SCRIPT_DIR / "nas-migrate-state.py")

        self.assertIn("--dry-run", migration)
        self.assertIn("--apply", migration)
        self.assertIn("/dev/cardpulse-at", migration)
        self.assertIn("first_seen_at", migration)
        self.assertIn("last_success", migration)
        self.assertIn("history.log", migration)
        self.assertIn("backup", migration)
        self.assertNotIn("systemctl enable", migration)
        self.assertNotIn("--delete-sms", migration)

    def test_migration_dry_run_is_non_destructive_and_filters_legacy_history(self) -> None:
        migration = SCRIPT_DIR / "nas-migrate-state.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "export"
            config_dir = source / "config"
            state_dir = source / "state"
            config_dir.mkdir(parents=True)
            state_dir.mkdir()
            (config_dir / "config.yaml").write_text(
                "serial:\n  port: /dev/ttyUSB2\n  auto_detect: true\n",
                encoding="utf-8",
            )
            (state_dir / "messages.jsonl").write_text(
                "\n".join(
                    [
                        '{"id":"keep","first_seen_at":"2026-07-10T00:00:00+00:00"}',
                        '{"id":"drop"}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            target = root / "target"
            result = subprocess.run(
                [
                    sys.executable,
                    str(migration),
                    "--source",
                    str(source),
                    "--target",
                    str(target),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("history records with trusted first_seen_at: 1", result.stdout)
            self.assertFalse(target.exists())

    def test_migration_config_filter_handles_only_unknown_fields(self) -> None:
        migration = load_migration_module()

        config_text = migration.rewrite_serial_config(
            migration.supported_config_text(
                "legacy:\n  wsl_path: /mnt/c/secret\n"
            )
        )

        self.assertEqual(
            config_text,
            "serial:\n  port: /dev/cardpulse-at\n  auto_detect: false\n",
        )

    def test_migration_only_keeps_recent_trusted_history_records(self) -> None:
        migration = load_migration_module()
        now = datetime(2026, 7, 10, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = Path(tmpdir) / "messages.jsonl"
            history_path.write_text(
                "\n".join(
                    [
                        json.dumps({"id": "recent", "first_seen_at": (now - timedelta(days=89)).isoformat()}),
                        json.dumps({"id": "expired", "first_seen_at": (now - timedelta(days=90)).isoformat()}),
                        json.dumps({"id": "future", "first_seen_at": (now + timedelta(minutes=1)).isoformat()}),
                        json.dumps({"id": "legacy"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            records = migration.valid_history_records(history_path, now=now)

            self.assertEqual([record["id"] for record in records], ["recent"])

    def test_migration_atomic_write_does_not_follow_a_predictable_temp_symlink(self) -> None:
        migration = load_migration_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            destination = root / "messages.jsonl"
            sensitive = root / "sensitive.txt"
            sensitive.write_text("do not overwrite\n", encoding="utf-8")
            predictable_temp = destination.with_suffix(destination.suffix + ".tmp")
            try:
                predictable_temp.symlink_to(sensitive)
            except OSError:
                self.skipTest("symbolic links are unavailable on this platform")

            migration.atomic_write(destination, '{"id":"safe"}\n')

            self.assertEqual(sensitive.read_text(encoding="utf-8"), "do not overwrite\n")
            self.assertFalse(destination.is_symlink())
            self.assertEqual(destination.read_text(encoding="utf-8"), '{"id":"safe"}\n')

    def test_migration_apply_rejects_an_unexpected_target_before_writing(self) -> None:
        migration = load_migration_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "export"
            source.mkdir()
            unexpected_target = root / "not-cardpulse"
            command = [
                "nas-migrate-state.py",
                "--source",
                str(source),
                "--target",
                str(unexpected_target),
                "--apply",
            ]

            with patch.object(migration.os, "geteuid", return_value=0, create=True):
                with patch.object(sys, "argv", command):
                    with self.assertRaises(SystemExit) as context:
                        migration.main()

            self.assertEqual(context.exception.code, 2)
            self.assertFalse(unexpected_target.exists())

    def test_origin_helper_normalizes_safe_https_origins_and_rejects_caddy_injection(self) -> None:
        helper = SCRIPT_DIR / "nas-origin.py"

        valid = subprocess.run(
            [sys.executable, str(helper), "https://CardPulse.NAS:443/"],
            capture_output=True,
            text=True,
            check=False,
        )
        invalid = subprocess.run(
            [sys.executable, str(helper), "https://evil.example\nrespond 200"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
        self.assertEqual(
            valid.stdout.splitlines(),
            ["https://cardpulse.nas", "cardpulse.nas"],
        )
        self.assertNotEqual(invalid.returncode, 0)

    def test_migration_uses_the_cardpulse_owner_for_state_and_backup(self) -> None:
        migration = load_migration_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "export"
            source.mkdir()
            target = root / "cardpulse"
            command = [
                "nas-migrate-state.py",
                "--source",
                str(source),
                "--target",
                str(target),
                "--apply",
            ]

            with patch.object(migration.os, "geteuid", return_value=0, create=True):
                with patch.object(migration, "NAS_STATE_ROOT", target):
                    with patch.object(migration, "resolve_cardpulse_owner", return_value=(1234, 5678)):
                        with patch.object(migration.os, "chown", create=True) as chown:
                            with patch.object(migration.os, "fchown", create=True):
                                with patch.object(sys, "argv", command):
                                    self.assertEqual(migration.main(), 0)

            owned_paths = {Path(call.args[0]) for call in chown.call_args_list}
            self.assertIn(target, owned_paths)
            self.assertIn(target / "config", owned_paths)
            self.assertIn(target / "state", owned_paths)
            self.assertIn(target / "backup", owned_paths)
            self.assertTrue(
                all(call.args[1:] == (1234, 5678) for call in chown.call_args_list)
            )

    def test_migration_apply_is_repeatable_private_and_only_copies_supported_config(self) -> None:
        migration = load_migration_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "export"
            config_dir = source / "config"
            state_dir = source / "state"
            config_dir.mkdir(parents=True)
            state_dir.mkdir()
            (config_dir / "config.yaml").write_text(
                "\n".join(
                    [
                        "serial:",
                        "  port: /dev/ttyUSB2",
                        "  baudrate: 115200",
                        "  auto_detect: true",
                        "sms:",
                        '  phone: "+8613800138000"',
                        '  message: "keepalive"',
                        "  interval_days: 179",
                        "  timeout: 30",
                        "  obsolete_transport: wsl-only",
                        "retry:",
                        "  max_attempts: 1",
                        "  interval: 1",
                        "logging:",
                        "  level: INFO",
                        "  file: /mnt/c/legacy-cardpulse.log",
                        "unrelated:",
                        "  legacy_path: /mnt/c/secret",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (state_dir / "last_success").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")
            (state_dir / "history.log").write_text("success\n", encoding="utf-8")
            (state_dir / "messages.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "id": "keep",
                                "body": "private",
                                "first_seen_at": "2026-07-10T00:00:00+00:00",
                            }
                        ),
                        json.dumps({"id": "legacy", "body": "discard"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            target = root / "cardpulse"
            command = [
                "nas-migrate-state.py",
                "--source",
                str(source),
                "--target",
                str(target),
                "--apply",
            ]

            with patch.object(migration.os, "geteuid", return_value=0, create=True):
                with patch.object(migration, "NAS_STATE_ROOT", target):
                    with patch.object(migration, "resolve_cardpulse_owner", return_value=(1234, 5678)):
                        with patch.object(migration.os, "chown", create=True):
                            with patch.object(migration.os, "fchown", create=True):
                                with patch.object(sys, "argv", command):
                                    first = migration.main()
                                with patch.object(sys, "argv", command):
                                    second = migration.main()

            self.assertEqual(first, 0)
            self.assertEqual(second, 0)
            migrated_config = (target / "config" / "config.yaml").read_text(encoding="utf-8")
            self.assertIn("port: /dev/cardpulse-at", migrated_config)
            self.assertIn("auto_detect: false", migrated_config)
            self.assertIn("phone:", migrated_config)
            self.assertIn("+8613800138000", migrated_config)
            self.assertNotIn("unrelated:", migrated_config)
            self.assertNotIn("/mnt/c/secret", migrated_config)
            self.assertNotIn("obsolete_transport", migrated_config)
            self.assertNotIn("logging:", migrated_config)
            self.assertNotIn("legacy-cardpulse.log", migrated_config)
            self.assertEqual(
                (target / "state" / "last_success").read_text(encoding="utf-8"),
                "2026-01-01T00:00:00Z\n",
            )
            history = (target / "state" / "messages.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(history), 1)
            self.assertEqual(json.loads(history[0])["id"], "keep")
            backups = list((target / "backup").iterdir())
            self.assertGreaterEqual(len(backups), 2)
            self.assertTrue(any((backup / "config.yaml").is_file() for backup in backups))
            if os.name != "nt":
                self.assertEqual((target / "backup").stat().st_mode & 0o777, 0o700)
                self.assertEqual((target / "state" / "messages.jsonl").stat().st_mode & 0o777, 0o600)

    def test_nas_production_documentation_has_safe_cutover_boundaries(self) -> None:
        readme = read_text(README_PATH)
        nas_doc = read_text(NAS_DOC_PATH)
        web_control_doc = read_text(WEB_CONTROL_DOC_PATH)

        self.assertIn("飞牛 NAS", readme)
        self.assertIn("满仓只会告警", readme)
        self.assertIn("飞牛 NAS 是唯一生产宿主", nas_doc)
        self.assertIn("手动上传 `.fpk`", nas_doc)
        self.assertIn("/app/cardpulse", nas_doc)
        self.assertIn("不开放 LAN TCP", nas_doc)
        self.assertIn("不使用 Caddy", nas_doc)
        self.assertIn("**no-device**", nas_doc)
        self.assertIn("fnos-readonly-poc.sh", nas_doc)
        self.assertIn("--record-acceptance", nas_doc)
        self.assertIn("`scheduler.enabled`", nas_doc)
        self.assertIn("旧容器已经完全停止", nas_doc)
        self.assertRegex(nas_doc, r"不得用 lifecycle\s+脚本调用 Docker CLI")
        self.assertIn("不发送短信、不删除短信", nas_doc)
        self.assertIn("只告警，不会自动清理", nas_doc)
        self.assertIn("QDC507 已接入 56", nas_doc)
        self.assertIn("2ca3:4006", nas_doc)
        self.assertIn("Vendor Specific interface", nas_doc)
        self.assertIn("/var/lib/cardpulse/lifecycle/qdc507-readonly-acceptance.json", nas_doc)
        self.assertIn("/var/lib/cardpulse/lifecycle/qdc507-readonly-acceptance.json", web_control_doc)
        self.assertNotIn("/var/lib/cardpulse/state/qdc507-readonly-acceptance.json", nas_doc)
        self.assertNotIn("/var/lib/cardpulse/state/qdc507-readonly-acceptance.json", web_control_doc)
        self.assertNotIn("sudo bash scripts/nas-install.sh", nas_doc)
        self.assertNotIn("CARDPULSE_WEB_PUBLIC_ORIGIN", nas_doc)


if __name__ == "__main__":
    unittest.main()
