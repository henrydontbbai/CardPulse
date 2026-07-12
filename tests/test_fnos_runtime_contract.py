#!/usr/bin/env python3
"""Static contract for the fnOS application image."""

import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "lib"))

import cardpulse_web  # noqa: E402


class FnosRuntimeContractTest(unittest.TestCase):
    def test_application_image_runs_web_over_unix_socket(self):
        dockerfile = (ROOT_DIR / "Dockerfile").read_text(encoding="utf-8")
        entrypoint = (ROOT_DIR / "scripts" / "fnos-entrypoint.sh").read_text(encoding="utf-8")

        self.assertIn("lib/cardpulse_web.py", dockerfile)
        self.assertIn("lib/web_auth.py", dockerfile)
        self.assertIn("web/index.html", dockerfile)
        self.assertIn("tini", dockerfile)
        self.assertIn("fnos-entrypoint.sh", dockerfile)
        self.assertIn("--socket", entrypoint)
        self.assertIn("--fnos-gateway", entrypoint)

    def test_image_bootstrap_directories_match_the_entrypoint_permission_contract(self):
        dockerfile = (ROOT_DIR / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("chmod 3770 /var/lib/cardpulse /run/cardpulse", dockerfile)
        self.assertIn("chmod 2750 /var/lib/cardpulse/config", dockerfile)
        self.assertIn("chmod 2770 /var/lib/cardpulse/state", dockerfile)

    def test_scheduler_is_persisted_and_disabled_by_default(self):
        entrypoint = (ROOT_DIR / "scripts" / "fnos-entrypoint.sh").read_text(encoding="utf-8")
        scheduler = (ROOT_DIR / "scripts" / "fnos-scheduler.sh").read_text(encoding="utf-8")

        self.assertIn("scheduler:\\n  enabled: false", entrypoint)
        self.assertIn("disable_scheduler_for_runtime_start", entrypoint)
        self.assertIn("if disable_scheduler_for_runtime_start; then", entrypoint)
        self.assertIn("CARDPULSE_QDC507_ACCEPTANCE_PATH", scheduler)
        self.assertIn("scheduler_enabled_from_config", scheduler)
        self.assertIn("keepalive_config_error", scheduler)
        self.assertIn("qdc507_readonly_acceptance_status", scheduler)
        self.assertIn("--check", scheduler)
        self.assertIn("cardpulse", scheduler)

    def test_fnos_runtime_preserves_package_private_volume_ownership(self):
        dockerfile = (ROOT_DIR / "Dockerfile").read_text(encoding="utf-8")
        entrypoint = (ROOT_DIR / "scripts" / "fnos-entrypoint.sh").read_text(encoding="utf-8")
        install_init = (ROOT_DIR / "packaging" / "fnos" / "cardpulse" / "cmd" / "install_init").read_text(
            encoding="utf-8"
        )
        config_permissions = (
            ROOT_DIR / "packaging" / "fnos" / "cardpulse" / "cmd" / "config-permissions.sh"
        ).read_text(encoding="utf-8")
        device_mode = (
            ROOT_DIR / "packaging" / "fnos" / "cardpulse" / "cmd" / "cardpulse-device-mode.sh"
        ).read_text(encoding="utf-8")

        self.assertNotIn('chown -R cardpulse:cardpulse "$DATA_DIR"', entrypoint)
        self.assertIn('data_gid=$(stat -c \'%g\' "$DATA_DIR")', entrypoint)
        self.assertIn('append_group "$data_gid"', entrypoint)
        self.assertIn('CONTROL_DIR="$DATA_DIR/lifecycle"', entrypoint)
        self.assertIn('chmod 3770 "$DATA_DIR"', entrypoint)
        self.assertIn('chmod 2770 "$STATE_DIR"', entrypoint)
        self.assertIn('chgrp "$data_gid" "$CONFIG_PATH"', entrypoint)
        self.assertIn('chmod 660 "$CONFIG_PATH"', entrypoint)
        self.assertNotIn('chmod 600 "$CONFIG_DIR/config.yaml"', entrypoint)
        self.assertIn('validate_lifecycle_control_for_runtime', entrypoint)
        self.assertIn('CARDPULSE_QDC507_DEVICE_MODE_PATH="$CONTROL_DIR/qdc507-device-mode.active"', entrypoint)
        self.assertNotIn('${CARDPULSE_QDC507_DEVICE_MODE_PATH:-', entrypoint)
        self.assertIn("config-permissions.sh", install_init)
        self.assertIn('mkdir -p "${TRIM_PKGVAR}/state" "${TRIM_APPDEST}"', install_init)
        self.assertIn('chmod 2770 "${TRIM_PKGVAR}" "${TRIM_PKGVAR}/state"', install_init)
        self.assertIn("cardpulse_prepare_config_directory", config_permissions)
        self.assertIn('chmod 2750 "$config_dir"', config_permissions)
        self.assertIn('CONTROL_DIR="${TRIM_PKGVAR}/lifecycle"', device_mode)
        self.assertIn('chmod 3770 "$TRIM_PKGVAR"', device_mode)
        self.assertIn('mkdir -m 2750 "$CONTROL_DIR"', device_mode)
        self.assertIn('"600"', device_mode)
        self.assertIn('"640"', device_mode)
        self.assertIn("CARDPULSE_FNOS_RUNTIME", dockerfile)

    def test_runtime_supervises_both_web_and_scheduler_processes(self):
        entrypoint = (ROOT_DIR / "scripts" / "fnos-entrypoint.sh").read_text(encoding="utf-8")

        self.assertTrue(entrypoint.startswith("#!/bin/bash\n"))
        self.assertIn('wait -n "$web_pid" "$scheduler_pid"', entrypoint)
        self.assertNotIn('trap \'stop_children; exit 0\' INT TERM\nwait "$scheduler_pid"', entrypoint)
        self.assertIn("stop_children\nexit 1", entrypoint)

    def test_runtime_dispatches_explicit_cli_commands(self):
        entrypoint = (ROOT_DIR / "scripts" / "fnos-entrypoint.sh").read_text(encoding="utf-8")

        self.assertIn('if [ "$#" -gt 0 ]; then\n    exec /usr/local/bin/cardpulse "$@"\nfi', entrypoint)

    def test_fnos_runtime_uses_only_the_pinned_device_and_drops_privileges(self):
        dockerfile = (ROOT_DIR / "Dockerfile").read_text(encoding="utf-8")
        entrypoint = (ROOT_DIR / "scripts" / "fnos-entrypoint.sh").read_text(encoding="utf-8")
        web_server = (ROOT_DIR / "lib" / "cardpulse_web.py").read_text(encoding="utf-8")
        config = (ROOT_DIR / "config" / "config.fnos.example.yaml").read_text(encoding="utf-8")

        self.assertIn("config/config.fnos.example.yaml", dockerfile)
        self.assertIn("CARDPULSE_WEB_CARDPULSE_BIN=/usr/local/bin/cardpulse", dockerfile)
        self.assertIn("ARG CARDPULSE_RELEASE_VERSION=1.1.0", dockerfile)
        self.assertIn("CARDPULSE_RUNTIME_VERSION=${CARDPULSE_RELEASE_VERSION}", dockerfile)
        self.assertIn("CARDPULSE_VERSION=${CARDPULSE_RELEASE_VERSION}", dockerfile)
        self.assertIn("cp /opt/cardpulse/config/config.fnos.example.yaml", entrypoint)
        self.assertNotIn('chown -R cardpulse:cardpulse "$DATA_DIR"', entrypoint)
        self.assertNotIn('chown cardpulse:"$socket_gid" "$SOCKET_DIR"', entrypoint)
        self.assertIn('chmod 3770 "$DATA_DIR"', entrypoint)
        self.assertIn('chmod 2770 "$STATE_DIR"', entrypoint)
        self.assertIn('chmod 3770 "$SOCKET_DIR"', entrypoint)
        self.assertIn('chmod go-rwx "$SOCKET_DIR/docker"', entrypoint)
        self.assertIn('setpriv --reuid=cardpulse --regid=cardpulse --groups="$group_list"', entrypoint)
        self.assertNotIn('VERSION_MARKER="$STATE_DIR/runtime-version"', entrypoint)
        self.assertNotIn('VERSION_MARKER="$CONTROL_DIR/runtime-version"', entrypoint)
        self.assertNotIn('RUNTIME_VERSION="${CARDPULSE_RUNTIME_VERSION:-unknown}"', entrypoint)
        self.assertIn('CARDPULSE_RUNTIME_VERSION=${CARDPULSE_RELEASE_VERSION}', dockerfile)
        self.assertIn("disable_scheduler_for_runtime_start", entrypoint)
        self.assertIn("CARDPULSE_SCHEDULER_RUNTIME_DISARMED=false", entrypoint)
        self.assertIn("CARDPULSE_SCHEDULER_RUNTIME_DISARMED=true", entrypoint)
        self.assertIn("def ensure_gateway_socket_directory", web_server)
        self.assertIn('port: "/dev/cardpulse-at"', config)
        self.assertIn("auto_detect: false", config)
        self.assertIn('phone: ""', config)
        self.assertIn('message: ""', config)
        self.assertIn("scheduler:\n  enabled: false", config)

    @unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"), "requires Unix sockets")
    def test_gateway_socket_inherits_the_gateway_group(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_dir = Path(tmpdir) / "gateway"
            socket_path = socket_dir / "app.sock"

            cardpulse_web.ensure_gateway_socket_directory(socket_dir)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(socket_path))
                directory_mode = socket_dir.stat().st_mode
                self.assertTrue(directory_mode & stat.S_ISGID)
                self.assertTrue(directory_mode & stat.S_ISVTX)
                self.assertEqual(socket_path.stat().st_gid, socket_dir.stat().st_gid)
            finally:
                server.close()

    def test_scheduler_setting_is_explicit_and_persistent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("serial: {}\nscheduler:\n  enabled: false\n", encoding="utf-8")

            self.assertFalse(cardpulse_web.scheduler_enabled_from_config(config_path))
            cardpulse_web.set_scheduler_enabled(config_path, True)

            self.assertTrue(cardpulse_web.scheduler_enabled_from_config(config_path))

    @unittest.skipUnless(os.name == "posix", "requires POSIX file permissions")
    def test_fnos_atomic_config_writes_remain_available_to_lifecycle_hooks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            config_path = config_dir / "config.yaml"
            config_path.write_text("scheduler:\n  enabled: true\n", encoding="utf-8")
            config_path.chmod(0o660)
            config_dir.chmod(0o550)

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "CARDPULSE_FNOS_RUNTIME": "1",
                        "CARDPULSE_CONFIG_DIR": str(config_dir),
                    },
                    clear=False,
                ),
                mock.patch.object(cardpulse_web, "ensure_private_directory"),
            ):
                cardpulse_web.atomic_write_private_text(config_path, "scheduler:\n  enabled: false\n")

            self.assertEqual(config_path.stat().st_mode & 0o777, 0o660)
            self.assertEqual(config_path.read_text(encoding="utf-8"), "scheduler:\n  enabled: false\n")

    @unittest.skipUnless(os.name == "posix", "requires POSIX file permissions")
    def test_fnos_atomic_config_write_rejects_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / "config"
            config_dir.mkdir()
            external_config = root / "external-config.yaml"
            external_config.write_text("scheduler:\n  enabled: true\n", encoding="utf-8")
            config_path = config_dir / "config.yaml"
            config_path.symlink_to(external_config)

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "CARDPULSE_FNOS_RUNTIME": "1",
                        "CARDPULSE_CONFIG_DIR": str(config_dir),
                    },
                    clear=False,
                ),
                mock.patch.object(cardpulse_web, "ensure_private_directory"),
            ):
                with self.assertRaises(PermissionError):
                    cardpulse_web.atomic_write_private_text(config_path, "scheduler:\n  enabled: false\n")

            self.assertTrue(config_path.is_symlink())
            self.assertIn("enabled: true", external_config.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_scheduler_rechecks_qdc507_acceptance_and_current_device_before_each_send(self):
        scheduler = ROOT_DIR / "scripts" / "fnos-scheduler.sh"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / "config"
            state_dir = root / "state"
            config_dir.mkdir()
            state_dir.mkdir()
            (config_dir / "config.yaml").write_text(
                "serial:\n"
                "  port: /dev/cardpulse-at\n"
                "  auto_detect: false\n"
                "sms:\n"
                "  phone: '+15551234567'\n"
                "  message: keepalive\n"
                "  interval_days: 179\n"
                "scheduler:\n"
                "  enabled: true\n",
                encoding="utf-8",
            )
            acceptance_path = state_dir / "qdc507-readonly-acceptance.json"
            environment = {
                **os.environ,
                "CARDPULSE_CONFIG_DIR": str(config_dir),
                "CARDPULSE_QDC507_ACCEPTANCE_PATH": str(acceptance_path),
                "CARDPULSE_LIB_DIR": str(ROOT_DIR / "lib"),
                "CARDPULSE_SCHEDULER_RUNTIME_DISARMED": "true",
            }

            def check_gate() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["sh", str(scheduler), "--check"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                    env=environment,
                )

            missing = check_gate()
            self.assertEqual(0, missing.returncode, missing.stderr)
            self.assertEqual("false", missing.stdout.strip())

            acceptance_path.write_text("{}\n", encoding="utf-8")
            invalid = check_gate()
            self.assertEqual(0, invalid.returncode, invalid.stderr)
            self.assertEqual("false", invalid.stdout.strip())

            acceptance_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "device": "/dev/cardpulse-at",
                        "commands": ["--doctor", "--info", "--sms-status", "--status"],
                        "nonroot": True,
                        "gateway": True,
                        "completed_at": "not-a-timestamp",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            invalid_timestamp = check_gate()
            self.assertEqual(0, invalid_timestamp.returncode, invalid_timestamp.stderr)
            self.assertEqual("false", invalid_timestamp.stdout.strip())

            acceptance_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "device": "/dev/cardpulse-at",
                        "resolved_device": "/dev/ttyUSB2",
                        "usb_id": "2ca3:4006",
                        "runtime_version": "1.1.0",
                        "commands": ["--doctor", "--info", "--sms-status", "--status"],
                        "nonroot": True,
                        "socket_backend": True,
                        "device_mode": "enabled",
                        "completed_at": "2026-07-11T00:00:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            acceptance_path.chmod(0o600)
            (state_dir / "runtime-version").write_text("1.1.0\n", encoding="utf-8")
            (state_dir / "qdc507-device-mode.active").write_text("enabled\n", encoding="utf-8")
            marker_without_current_device = check_gate()
            self.assertEqual(0, marker_without_current_device.returncode, marker_without_current_device.stderr)
            self.assertEqual("false", marker_without_current_device.stdout.strip())

            (config_dir / "config.yaml").write_text(
                "serial:\n"
                "  port: /dev/ttyUSB0\n"
                "  auto_detect: true\n"
                "scheduler:\n"
                "  enabled: true\n",
                encoding="utf-8",
            )
            invalid_config = check_gate()
            self.assertEqual(0, invalid_config.returncode, invalid_config.stderr)
            self.assertEqual("false", invalid_config.stdout.strip())

            acceptance_path.unlink()
            removed = check_gate()
            self.assertEqual(0, removed.returncode, removed.stderr)
            self.assertEqual("false", removed.stdout.strip())

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_scheduler_refuses_to_send_without_a_successful_runtime_disarm(self):
        scheduler = ROOT_DIR / "scripts" / "fnos-scheduler.sh"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / "config"
            state_dir = root / "state"
            config_dir.mkdir()
            state_dir.mkdir()
            (config_dir / "config.yaml").write_text(
                "serial:\n"
                "  port: /dev/cardpulse-at\n"
                "  auto_detect: false\n"
                "sms:\n"
                "  phone: '+15551234567'\n"
                "  message: keepalive\n"
                "  interval_days: 179\n"
                "scheduler:\n"
                "  enabled: true\n",
                encoding="utf-8",
            )
            acceptance_path = state_dir / "qdc507-readonly-acceptance.json"
            acceptance_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "device": "/dev/cardpulse-at",
                        "commands": ["--doctor", "--info", "--sms-status", "--status"],
                        "nonroot": True,
                        "gateway": True,
                        "completed_at": "2026-07-11T00:00:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["sh", str(scheduler), "--check"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
                env={
                    **os.environ,
                    "CARDPULSE_CONFIG_DIR": str(config_dir),
                    "CARDPULSE_QDC507_ACCEPTANCE_PATH": str(acceptance_path),
                    "CARDPULSE_LIB_DIR": str(ROOT_DIR / "lib"),
                    "CARDPULSE_SCHEDULER_RUNTIME_DISARMED": "false",
                },
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("false", completed.stdout.strip())

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_entrypoint_disarm_write_failure_never_reaches_scheduler_send(self):
        """A failed startup disarm must leave the running scheduler fail-closed."""
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("requires Bash, as does the production entrypoint")
        entrypoint_source = (ROOT_DIR / "scripts" / "fnos-entrypoint.sh").read_text(encoding="utf-8")
        scheduler_source = (ROOT_DIR / "scripts" / "fnos-scheduler.sh").read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bin_dir = root / "bin"
            data_dir = root / "data"
            config_dir = data_dir / "config"
            state_dir = data_dir / "state"
            socket_path = root / "gateway" / "app.sock"
            entrypoint = root / "fnos-entrypoint.sh"
            scheduler = root / "fnos-scheduler.sh"
            send_log = root / "cardpulse-sends.log"
            bin_dir.mkdir()
            config_dir.mkdir(parents=True)
            state_dir.mkdir()

            (config_dir / "config.yaml").write_text(
                "serial:\n"
                "  port: /dev/cardpulse-at\n"
                "  auto_detect: false\n"
                "sms:\n"
                "  phone: '+15551234567'\n"
                "  message: keepalive\n"
                "  interval_days: 179\n"
                "scheduler:\n"
                "  enabled: true\n",
                encoding="utf-8",
            )
            scheduler.write_text(scheduler_source, encoding="utf-8")
            scheduler.chmod(0o755)

            # Keep the production entrypoint body intact, replacing only the absolute
            # in-image scheduler location with this isolated copy.
            self.assertIn("/opt/cardpulse/scripts/fnos-scheduler.sh", entrypoint_source)
            entrypoint.write_text(
                entrypoint_source.replace("/opt/cardpulse/scripts/fnos-scheduler.sh", str(scheduler)),
                encoding="utf-8",
            )
            entrypoint.chmod(0o755)

            (bin_dir / "id").write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = \"-u\" ]; then\n"
                "    printf '%s\\n' 1000\n"
                "    exit 0\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            (bin_dir / "python3").write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = \"-\" ]; then\n"
                "    if [ -n \"${RUNTIME_DISARMED+x}\" ]; then\n"
                "        printf '%s\\n' true\n"
                "        exit 0\n"
                "    fi\n"
                "    exit 1\n"
                "fi\n"
                "while :; do sleep 1; done\n",
                encoding="utf-8",
            )
            (bin_dir / "cardpulse").write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' called >> \"$CARDPULSE_FAKE_SEND_LOG\"\n",
                encoding="utf-8",
            )
            for helper in bin_dir.iterdir():
                helper.chmod(0o755)

            environment = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "CARDPULSE_DATA_DIR": str(data_dir),
                "CARDPULSE_WEB_SOCKET": str(socket_path),
                "CARDPULSE_LIB_DIR": str(ROOT_DIR / "lib"),
                "CARDPULSE_FAKE_SEND_LOG": str(send_log),
                "CARDPULSE_SCHEDULER_POLL_SECONDS": "0.05",
            }
            environment.pop("RUNTIME_DISARMED", None)
            environment.pop("CARDPULSE_SCHEDULER_RUNTIME_DISARMED", None)
            process = subprocess.Popen(
                [bash, str(entrypoint)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            initial_returncode = None
            try:
                time.sleep(0.25)
                initial_returncode = process.poll()
                self.assertFalse(send_log.exists(), "scheduler called cardpulse after startup disarm failed")
            finally:
                process.terminate()
                _stdout, stderr = process.communicate(timeout=3)

            self.assertIsNone(
                initial_returncode,
                f"entrypoint exited before the scheduler gate could be exercised: {stderr}",
            )
            self.assertIn("startup disarm failed", stderr)
            self.assertFalse(send_log.exists(), "scheduler called cardpulse after shutdown")

    def test_runtime_uses_only_a_lifecycle_owned_active_mode_file(self):
        source = (ROOT_DIR / "scripts" / "fnos-entrypoint.sh").read_text(encoding="utf-8")

        self.assertIn('CONTROL_DIR="$DATA_DIR/lifecycle"', source)
        self.assertIn('CONTROL_ACTIVE_MODE_FILE="$CONTROL_DIR/qdc507-device-mode.active"', source)
        self.assertIn('if [ -L "$CONTROL_DIR" ] || [ ! -d "$CONTROL_DIR" ]; then', source)
        self.assertIn('control_mode=$(stat -c \'%a\' "$CONTROL_DIR")', source)
        self.assertIn('[ "$control_mode" != "2750" ]', source)
        self.assertIn('if [ -L "$CONTROL_ACTIVE_MODE_FILE" ] || [ ! -f "$CONTROL_ACTIVE_MODE_FILE" ]; then', source)
        self.assertIn('active_mode=$(stat -c \'%a\' "$CONTROL_ACTIVE_MODE_FILE")', source)
        self.assertIn('[ "$active_mode" != "640" ]', source)
        self.assertIn('CARDPULSE_QDC507_DEVICE_MODE_PATH="/dev/null"', source)

    def test_runtime_refuses_an_untrusted_lifecycle_configuration_directory(self):
        source = (ROOT_DIR / "scripts" / "fnos-entrypoint.sh").read_text(encoding="utf-8")

        self.assertIn('if [ -L "$CONFIG_DIR" ] || [ ! -d "$CONFIG_DIR" ]; then', source)
        self.assertIn('config_uid=$(stat -c \'%u\' "$CONFIG_DIR")', source)
        self.assertIn('config_gid=$(stat -c \'%g\' "$CONFIG_DIR")', source)
        self.assertIn('config_mode=$(stat -c \'%a\' "$CONFIG_DIR")', source)
        self.assertIn('[ "$config_mode" != "2750" ]', source)
        self.assertNotIn('mkdir -p "$CONFIG_DIR" "$STATE_DIR" "$SOCKET_DIR"', source)

    def test_runtime_preserves_sticky_parent_and_does_not_create_or_chmod_lifecycle(self):
        source = (ROOT_DIR / "scripts" / "fnos-entrypoint.sh").read_text(encoding="utf-8")

        self.assertIn('chmod 3770 "$DATA_DIR"', source)
        self.assertNotIn('mkdir -p "$CONTROL_DIR"', source)
        self.assertNotIn('chmod 750 "$CONTROL_DIR"', source)
        self.assertNotIn('chmod 640 "$CONTROL_ACTIVE_MODE_FILE"', source)


if __name__ == "__main__":
    unittest.main()
