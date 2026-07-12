#!/usr/bin/env python3
"""Static safety contract for the manually uploaded fnOS FPK payload."""

import configparser
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT_DIR = Path(__file__).resolve().parents[1]
PACKAGE = ROOT_DIR / "packaging" / "fnos" / "cardpulse"


class FnosFpkContractTest(unittest.TestCase):
    IMAGE = "registry.example/cardpulse@sha256:" + "a" * 64

    @staticmethod
    def _tar_add_file(archive: tarfile.TarFile, name: str, content: str | bytes) -> None:
        payload = content.encode("utf-8") if isinstance(content, str) else content
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(payload))

    def _write_fpk(
        self,
        path: Path,
        app_files: dict[str, str | bytes],
        package_files: dict[str, str | bytes] | None = None,
    ) -> None:
        app_buffer = io.BytesIO()
        with tarfile.open(fileobj=app_buffer, mode="w:gz") as app_archive:
            for name, content in app_files.items():
                self._tar_add_file(app_archive, name, content)

        package_files = package_files or self._safe_package_files()
        with tarfile.open(path, mode="w:gz") as archive:
            self._tar_add_file(archive, "app.tgz", app_buffer.getvalue())
            for name, content in package_files.items():
                self._tar_add_file(archive, name, content)

    def _verify_fpk(
        self,
        path: Path,
        image: str | None = None,
        version: str | None = "1.1.0",
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(ROOT_DIR / "scripts" / "verify-fnos-fpk.py"),
            str(path),
            "--image",
            image or self.IMAGE,
        ]
        if version is not None:
            command.extend(("--version", version))
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )

    def _safe_package_files(self) -> dict[str, str | bytes]:
        source_files = (
            "manifest",
            "ICON.PNG",
            "ICON_256.PNG",
            "config/privilege",
            "config/resource",
            "wizard/.keep",
            "wizard/install",
            "wizard/config",
            "wizard/uninstall",
            "cmd/main",
            "cmd/install_init",
            "cmd/install_callback",
            "cmd/upgrade_init",
            "cmd/upgrade_callback",
            "cmd/uninstall_init",
            "cmd/uninstall_callback",
            "cmd/config_init",
            "cmd/config_callback",
            "cmd/config-permissions.sh",
            "cmd/migration-guard.sh",
            "cmd/cardpulse-device-mode.sh",
            "cmd/disarm-scheduler.sh",
        )
        return {
            name: (PACKAGE / name).read_bytes() for name in source_files
        }

    def _safe_app_files(
        self,
        image: str | None = None,
        version: str = "1.1.0",
    ) -> dict[str, str | bytes]:
        image = image or self.IMAGE
        no_device_compose = "\n".join(
            (
                "services:",
                "  cardpulse:",
                f"    image: {image}",
                "    container_name: cardpulse",
                "    restart: unless-stopped",
                "    environment:",
                "      CARDPULSE_DATA_DIR: /var/lib/cardpulse",
                "      CARDPULSE_WEB_SOCKET: /run/cardpulse/app.sock",
                "      CARDPULSE_WEB_BASE_PATH: /app/cardpulse",
                '      CARDPULSE_FNOS_RUNTIME: "1"',
                f'      CARDPULSE_RUNTIME_IMAGE: "{image}"',
                f'      CARDPULSE_FPK_VERSION: "{version}"',
                '      CARDPULSE_SCHEDULER_POLL_SECONDS: "3600"',
                "    volumes:",
                "      - ${TRIM_PKGVAR}:/var/lib/cardpulse",
                "      - ${TRIM_APPDEST}:/run/cardpulse",
                "    security_opt:",
                "      - no-new-privileges:true",
            )
        )
        device_compose = no_device_compose + "\n    devices:\n      - /dev/cardpulse-at:/dev/cardpulse-at\n"
        ui = json.dumps(
            {
                ".url": {
                    "cardpulse.Application": {
                        "type": "iframe",
                        "protocol": "",
                        "gatewayPrefix": "/app/cardpulse",
                        "gatewaySocket": "app.sock",
                        "url": "/app/cardpulse",
                        "allUsers": False,
                    }
                }
            }
        )
        return {
            "config/privilege": (PACKAGE / "config" / "privilege").read_bytes(),
            "config/resource": (PACKAGE / "config" / "resource").read_bytes(),
            "docker/docker-compose.yaml": no_device_compose,
            "docker/docker-compose.no-device.yaml": no_device_compose,
            "docker/docker-compose.device.yaml": device_compose,
            "ui/config": ui,
            "ui/images/icon_64.png": b"icon",
            "ui/images/icon_256.png": b"icon",
            "diagnostics/fnos-readonly-poc.sh": (
                ROOT_DIR / "scripts" / "fnos-readonly-poc.sh"
            ).read_bytes(),
            "diagnostics/fnos-platform-poc.sh": (
                ROOT_DIR / "scripts" / "fnos-platform-poc.sh"
            ).read_bytes(),
        }

    def test_manifest_has_no_duplicate_options(self):
        manifest = (PACKAGE / "manifest").read_text(encoding="utf-8")
        parser = configparser.ConfigParser(strict=True)

        parser.read_string("[package]\n" + manifest)

        self.assertEqual("cardpulse", parser["package"]["appname"])
        self.assertEqual("true", parser["package"]["disable_authorization_path"])

    def test_verifier_accepts_an_explicit_upgrade_release_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            package_files = self._safe_package_files()
            package_files["manifest"] = package_files["manifest"].replace(
                b"version               = 1.1.0",
                b"version               = 1.1.1",
            )
            self._write_fpk(fpk, self._safe_app_files(version="1.1.1"), package_files)

            result = self._verify_fpk(fpk, version="1.1.1")

        self.assertEqual(0, result.returncode, result.stderr)

    def test_package_uses_gateway_private_storage_and_safe_explicit_device_overlay(self):
        docker_dir = PACKAGE / "app/docker"
        default_compose = (docker_dir / "docker-compose.yaml").read_text(encoding="utf-8")
        no_device_compose = (docker_dir / "docker-compose.no-device.yaml").read_text(encoding="utf-8")
        device_compose = (docker_dir / "docker-compose.device.yaml").read_text(encoding="utf-8")
        ui = json.loads((PACKAGE / "app/ui/config").read_text(encoding="utf-8"))

        for compose in (default_compose, no_device_compose, device_compose):
            self.assertIn("${TRIM_PKGVAR}:/var/lib/cardpulse", compose)
            self.assertIn("${TRIM_APPDEST}:/run/cardpulse", compose)
            self.assertIn("CARDPULSE_WEB_BASE_PATH: /app/cardpulse", compose)
            self.assertIn('CARDPULSE_RUNTIME_IMAGE: "__CARDPULSE_RUNTIME_IMAGE__"', compose)
            self.assertIn('CARDPULSE_FPK_VERSION: "__CARDPULSE_FPK_VERSION__"', compose)
            self.assertNotRegex(compose, re.compile(r"(?m)^[ \t]*privileged[ \t]*:"))
            self.assertNotRegex(compose, re.compile(r"(?m)^[ \t]*ports[ \t]*:"))
            self.assertNotIn("/dev:/dev", compose)
            self.assertNotIn("/dev/bus/usb", compose)
            self.assertNotIn("ttyUSB", compose)

        for compose in (default_compose, no_device_compose):
            self.assertNotRegex(compose, re.compile(r"(?m)^[ \t]*devices[ \t]*:"))
            self.assertNotIn("/dev/cardpulse-at", compose)

        self.assertRegex(device_compose, re.compile(r"(?m)^[ \t]*devices[ \t]*:"))
        self.assertEqual(1, device_compose.count("/dev/cardpulse-at:/dev/cardpulse-at"))
        device_mappings = re.findall(r"(?m)^[ \t]*-[ \t]*/dev/[^ \t\r\n]+", device_compose)
        self.assertEqual(["      - /dev/cardpulse-at:/dev/cardpulse-at"], device_mappings)

        launcher = ui[".url"]["cardpulse.Application"]
        self.assertEqual("iframe", launcher["type"])
        self.assertEqual("", launcher["protocol"])
        self.assertEqual("/app/cardpulse", launcher["gatewayPrefix"])
        self.assertEqual("app.sock", launcher["gatewaySocket"])
        self.assertEqual("/app/cardpulse", launcher["url"])
        self.assertIs(False, launcher["allUsers"])

    def test_package_has_every_fnpack_required_asset_and_digest_build_gate(self):
        required_paths = (
            "ICON.PNG",
            "ICON_256.PNG",
            "app/ui/images/icon_64.png",
            "app/ui/images/icon_256.png",
            "wizard/.keep",
            "wizard/install",
            "wizard/config",
            "wizard/uninstall",
            "cmd/install_init",
            "cmd/install_callback",
            "cmd/upgrade_init",
            "cmd/upgrade_callback",
            "cmd/uninstall_init",
            "cmd/uninstall_callback",
            "cmd/config_init",
            "cmd/config_callback",
            "cmd/migration-guard.sh",
            "cmd/cardpulse-device-mode.sh",
            "app/docker/docker-compose.no-device.yaml",
            "app/docker/docker-compose.device.yaml",
        )
        for relative_path in required_paths:
            self.assertTrue((PACKAGE / relative_path).is_file(), relative_path)

        build_script = (ROOT_DIR / "scripts" / "build-fnos-fpk.sh").read_text(encoding="utf-8")
        self.assertIn("fnpack build", build_script)
        self.assertIn("@sha256:", build_script)
        self.assertIn("CARDPULSE_FPK_VERSION", build_script)
        self.assertIn('--version "$FPK_VERSION"', build_script)
        self.assertIn('release_filename="cardpulse-${FPK_VERSION}-sha256-${IMAGE_SHA256}.fpk"', build_script)
        self.assertIn("docker pull --platform linux/amd64", build_script)
        self.assertIn("docker image inspect", build_script)
        self.assertIn('"linux/amd64"', build_script)
        self.assertIn('"$stage_dir/app/diagnostics"', build_script)
        self.assertIn("fnos-readonly-poc.sh", build_script)
        compose_template = (PACKAGE / "app/docker/docker-compose.yaml").read_text(encoding="utf-8")
        self.assertIn("__CARDPULSE_IMAGE__", compose_template)
        self.assertIn("__CARDPULSE_RUNTIME_IMAGE__", compose_template)
        self.assertIn("__CARDPULSE_FPK_VERSION__", compose_template)

        verifier = (ROOT_DIR / "scripts" / "verify-fnos-fpk.py").read_text(encoding="utf-8")
        for required in (
            '"wizard/.keep"',
            '"wizard/install"',
            '"wizard/config"',
            '"wizard/uninstall"',
            '"cmd/migration-guard.sh"',
            '"cmd/cardpulse-device-mode.sh"',
            '"docker/docker-compose.no-device.yaml"',
            '"docker/docker-compose.device.yaml"',
            '"diagnostics/fnos-readonly-poc.sh"',
            '"gatewayPrefix"',
            '"gatewaySocket"',
            '"HostConfig.Privileged"',
            '"HostConfig.NetworkMode"',
            '"disable_authorization_path"',
            '"wizard_cardpulse_private_data"',
        ):
            self.assertIn(required, verifier)

        workflow = (ROOT_DIR / ".github/workflows/fnos-package.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("fpk_version", workflow)
        self.assertIn("--platform linux/amd64", workflow)
        self.assertIn("CARDPULSE_RELEASE_VERSION", workflow)
        self.assertIn("steps.fpk.outputs.path", workflow)
        self.assertIn("--metadata-file", workflow)
        self.assertIn("containerimage.digest", workflow)
        self.assertNotIn("imagetools inspect", workflow)
        self.assertIn("build-fnos-fpk.sh", workflow)
        self.assertIn("Run fnOS image smoke test", workflow)
        self.assertIn("--unix-socket", workflow)
        self.assertIn("cardpulse-fnos-smoke", workflow)
        self.assertIn("fnos-platform-poc.sh", workflow)
        self.assertIn("--record-persistence", workflow)
        self.assertIn("python3-yaml", workflow)

    @unittest.skipUnless(os.name == "posix", "requires POSIX build tools")
    def test_build_script_stages_versioned_digest_package_with_both_pocs(self):
        image = self.IMAGE
        version = "1.1.1"
        digest = image.rsplit("@sha256:", 1)[1]
        build_script = ROOT_DIR / "scripts" / "build-fnos-fpk.sh"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_bin = root / "bin"
            output_dir = root / "dist"
            fake_bin.mkdir()
            (fake_bin / "docker").write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "case \"$1\" in\n"
                "pull) exit 0 ;;\n"
                "image)\n"
                "    test \"${2:-}\" = inspect\n"
                "    printf '%s\\n' linux/amd64\n"
                "    ;;\n"
                "run)\n"
                "    test \"${2:-}\" = --rm\n"
                "    test \"${3:-}\" = --platform\n"
                "    test \"${4:-}\" = linux/amd64\n"
                "    test \"${5:-}\" = --entrypoint\n"
                "    test \"${6:-}\" = /usr/local/bin/cardpulse\n"
                "    test \"${7:-}\" = \"$CARDPULSE_IMAGE\"\n"
                "    test \"${8:-}\" = --version\n"
                "    printf '%s\\n' 'CardPulse 1.1.1'\n"
                "    ;;\n"
                "*) exit 91 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            (fake_bin / "fnpack").write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "test \"${1:-}\" = build\n"
                "mkdir -p app/config\n"
                "cp config/privilege config/resource app/config/\n"
                "tar -C app -czf app.tgz config docker ui diagnostics\n"
                "tar -czf cardpulse.fpk manifest ICON.PNG ICON_256.PNG config wizard cmd app.tgz\n"
                "rm app.tgz\n",
                encoding="utf-8",
            )
            for command in fake_bin.iterdir():
                command.chmod(0o755)

            completed = subprocess.run(
                ["sh", str(build_script)],
                check=False,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "CARDPULSE_IMAGE": image,
                    "CARDPULSE_FPK_VERSION": version,
                    "OUTPUT_DIR": str(output_dir),
                },
            )

            expected_path = output_dir / f"cardpulse-{version}-sha256-{digest}.fpk"
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(str(expected_path), completed.stdout.strip())
            self.assertTrue(expected_path.is_file())
            with tarfile.open(expected_path, "r:gz") as archive:
                manifest = archive.extractfile("manifest")
                app = archive.extractfile("app.tgz")
                self.assertIsNotNone(manifest)
                self.assertIsNotNone(app)
                assert manifest is not None
                assert app is not None
                self.assertIn("version               = 1.1.1", manifest.read().decode("utf-8"))
                with tarfile.open(fileobj=io.BytesIO(app.read()), mode="r:gz") as app_archive:
                    self.assertIsNotNone(app_archive.extractfile("diagnostics/fnos-readonly-poc.sh"))
                    self.assertIsNotNone(app_archive.extractfile("diagnostics/fnos-platform-poc.sh"))
                    compose = app_archive.extractfile("docker/docker-compose.yaml")
                    self.assertIsNotNone(compose)
                    assert compose is not None
                    rendered_compose = compose.read().decode("utf-8")
                    self.assertIn(f'CARDPULSE_RUNTIME_IMAGE: "{image}"', rendered_compose)
                    self.assertIn(f'CARDPULSE_FPK_VERSION: "{version}"', rendered_compose)

    @unittest.skipUnless(os.name == "posix", "requires POSIX build tools")
    def test_build_script_rejects_an_image_version_that_differs_from_fpk_version(self):
        image = self.IMAGE
        build_script = ROOT_DIR / "scripts" / "build-fnos-fpk.sh"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "docker").write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "case \"$1\" in\n"
                "pull) exit 0 ;;\n"
                "image)\n"
                "    test \"${2:-}\" = inspect\n"
                "    printf '%s\\n' linux/amd64\n"
                "    ;;\n"
                "run)\n"
                "    printf '%s\\n' 'CardPulse 1.1.1'\n"
                "    ;;\n"
                "*) exit 91 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            (fake_bin / "fnpack").write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'fnpack must not run after a release version mismatch' >&2\n"
                "exit 99\n",
                encoding="utf-8",
            )
            for command in fake_bin.iterdir():
                command.chmod(0o755)

            completed = subprocess.run(
                ["sh", str(build_script)],
                check=False,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "CARDPULSE_IMAGE": image,
                    "CARDPULSE_FPK_VERSION": "1.1.2",
                    "OUTPUT_DIR": str(root / "dist"),
                },
            )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("CardPulse --version", completed.stderr)
        self.assertIn("1.1.2", completed.stderr)

    def test_lifecycle_does_not_manage_host_modem_or_delete_data(self):
        lifecycle = "\n".join(
            (PACKAGE / "cmd" / name).read_text(encoding="utf-8")
            for name in (
                "install_init",
                "install_callback",
                "upgrade_init",
                "upgrade_callback",
                "uninstall_init",
                "uninstall_callback",
                "main",
                "migration-guard.sh",
            )
        ).lower()

        for forbidden in (
            "systemctl disable",
            "systemctl enable",
            "systemctl stop",
            "systemctl restart",
            "systemctl start",
            "systemctl daemon-reload",
            "crontab -r",
            "caddy",
            "rm -rf",
        ):
            self.assertNotIn(forbidden, lifecycle)
        for forbidden_command in ("udevadm", "modprobe"):
            self.assertNotRegex(
                lifecycle,
                re.compile(rf"(?m)^[ \t]*(?:sudo[ \t]+)?{forbidden_command}(?:[ \t]|$)"),
            )
        self.assertNotRegex(
            lifecycle,
            re.compile(
                r"(?m)^[ \t]*(?:sudo[ \t]+)?systemctl[ \t]+"
                r"(?:disable|enable|stop|restart|start|daemon-reload)(?:[ \t]|$)"
            ),
        )
        self.assertNotRegex(
            lifecycle,
            re.compile(
                r"(?m)^[ \t]*(?:sudo[ \t]+)?(?:service|rc-service)[ \t]+[^ \t]+[ \t]+"
                r"(?:stop|restart|start)(?:[ \t]|$)"
            ),
        )
        self.assertIn("trim_pkgvar", lifecycle)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_upgrade_init_disarms_the_persisted_scheduler_before_runtime_starts(self):
        disarm_script = PACKAGE / "cmd" / "disarm-scheduler.sh"
        upgrade_init = PACKAGE / "cmd" / "upgrade_init"
        self.assertTrue(disarm_script.is_file(), "missing scheduler disarm helper")
        self.assertIn("disarm-scheduler.sh", upgrade_init.read_text(encoding="utf-8"))
        self.assertIn("config-permissions.sh", upgrade_init.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmpdir:
            pkgvar = Path(tmpdir) / "private"
            config_dir = pkgvar / "config"
            config_dir.mkdir(parents=True)
            config_dir.chmod(0o2770)
            config_path = config_dir / "config.yaml"
            config_path.write_text(
                "serial:\n"
                "  port: /dev/cardpulse-at\n"
                "scheduler:\n"
                "  enabled: true\n"
                "  retained_note: preserve-me\n"
                "logging:\n"
                "  level: INFO\n",
                encoding="utf-8",
            )
            config_path.chmod(0o660)
            config_gid = config_dir.stat().st_gid

            result = subprocess.run(
                ["sh", str(disarm_script)],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "TRIM_PKGVAR": str(pkgvar)},
            )

            self.assertEqual(0, result.returncode, result.stderr)
            content = config_path.read_text(encoding="utf-8")
            self.assertIn("scheduler:\n  enabled: false\n", content)
            self.assertNotIn("enabled: true", content)
            self.assertIn("  retained_note: preserve-me\n", content)
            self.assertIn("logging:\n  level: INFO\n", content)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o660)
            self.assertEqual(config_path.stat().st_gid, config_gid)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_upgrade_init_disarms_before_a_legacy_migration_guard_rejects_upgrade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            command_dir = root / "cmd"
            config_dir = root / "private" / "config"
            command_dir.mkdir()
            config_dir.mkdir(parents=True)
            for name in ("upgrade_init", "disarm-scheduler.sh", "config-permissions.sh"):
                (command_dir / name).write_text(
                    (PACKAGE / "cmd" / name).read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            (command_dir / "migration-guard.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            config_path = config_dir / "config.yaml"
            config_path.write_text("scheduler:\n  enabled: true\n", encoding="utf-8")

            result = subprocess.run(
                ["sh", str(command_dir / "upgrade_init")],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "TRIM_PKGVAR": str(root / "private")},
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("scheduler:\n  enabled: false\n", config_path.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_upgrade_disarm_rejects_a_symlinked_configuration(self):
        disarm_script = PACKAGE / "cmd" / "disarm-scheduler.sh"
        with tempfile.TemporaryDirectory() as tmpdir:
            pkgvar = Path(tmpdir) / "private"
            config_dir = pkgvar / "config"
            config_dir.mkdir(parents=True)
            external_config = Path(tmpdir) / "external-config.yaml"
            external_config.write_text("scheduler:\n  enabled: true\n", encoding="utf-8")
            config_path = config_dir / "config.yaml"
            config_path.symlink_to(external_config)

            result = subprocess.run(
                ["sh", str(disarm_script)],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "TRIM_PKGVAR": str(pkgvar)},
            )

            self.assertNotEqual(0, result.returncode)
            self.assertTrue(config_path.is_symlink())
            self.assertIn("enabled: true", external_config.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_install_init_rejects_a_symlinked_retained_configuration_directory(self):
        install_init = PACKAGE / "cmd" / "install_init"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pkgvar = root / "private"
            pkgvar.mkdir()
            external_config_dir = root / "external-config"
            external_config_dir.mkdir()
            config_dir = pkgvar / "config"
            config_dir.symlink_to(external_config_dir, target_is_directory=True)

            result = subprocess.run(
                ["sh", str(install_init)],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "TRIM_PKGVAR": str(pkgvar), "TRIM_APPDEST": str(root / "appdest")},
            )

            self.assertNotEqual(0, result.returncode)
            self.assertTrue(config_dir.is_symlink())
            self.assertFalse((external_config_dir / "config.yaml").exists())

    def test_verifier_accepts_a_no_device_default_with_one_explicit_alias_overlay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            self._write_fpk(fpk, self._safe_app_files())

            result = self._verify_fpk(fpk)

        self.assertEqual(0, result.returncode, result.stderr)

    def test_verifier_requires_the_poc_container_name_and_restart_policy(self):
        for removed_line, expected in (
            ("    container_name: cardpulse\n", "container_name"),
            ("    restart: unless-stopped\n", "restart"),
        ):
            with self.subTest(removed_line=removed_line), tempfile.TemporaryDirectory() as tmpdir:
                fpk = Path(tmpdir) / "cardpulse.fpk"
                app_files = self._safe_app_files()
                for compose_name in (
                    "docker/docker-compose.yaml",
                    "docker/docker-compose.no-device.yaml",
                    "docker/docker-compose.device.yaml",
                ):
                    app_files[compose_name] = str(app_files[compose_name]).replace(removed_line, "")
                self._write_fpk(fpk, app_files)

                result = self._verify_fpk(fpk)

            self.assertNotEqual(0, result.returncode)
            self.assertIn(expected, result.stderr)

    def test_verifier_requires_the_packaged_readonly_poc(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            app_files = self._safe_app_files()
            del app_files["diagnostics/fnos-readonly-poc.sh"]
            self._write_fpk(fpk, app_files)

            result = self._verify_fpk(fpk)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("diagnostics/fnos-readonly-poc.sh", result.stderr)

    def test_verifier_requires_the_packaged_no_device_platform_poc(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            app_files = self._safe_app_files()
            del app_files["diagnostics/fnos-platform-poc.sh"]
            self._write_fpk(fpk, app_files)

            result = self._verify_fpk(fpk)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("diagnostics/fnos-platform-poc.sh", result.stderr)

    def test_verifier_requires_the_upgrade_scheduler_disarm_helper(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            package_files = self._safe_package_files()
            del package_files["cmd/disarm-scheduler.sh"]
            self._write_fpk(fpk, self._safe_app_files(), package_files)

            result = self._verify_fpk(fpk)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("cmd/disarm-scheduler.sh", result.stderr)

    def test_verifier_rejects_a_poc_without_runtime_privilege_checks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            app_files = self._safe_app_files()
            app_files["diagnostics/fnos-readonly-poc.sh"] = str(
                app_files["diagnostics/fnos-readonly-poc.sh"]
            ).replace("HostConfig.Privileged", "HostConfig.Missing")
            self._write_fpk(fpk, app_files)

            result = self._verify_fpk(fpk)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("HostConfig.Privileged", result.stderr)

    def test_verifier_rejects_an_obfuscated_non_readonly_poc_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            app_files = self._safe_app_files()
            app_files["diagnostics/fnos-readonly-poc.sh"] = (
                bytes(app_files["diagnostics/fnos-readonly-poc.sh"])
                + b'\nmode=--te"st"\nrun_as_cardpulse cardpulse "$mode"\n'
            )
            self._write_fpk(fpk, app_files)

            result = self._verify_fpk(fpk)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("reviewed POC source", result.stderr)

    def test_verifier_rejects_any_device_mapping_other_than_the_fixed_alias(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            app_files = self._safe_app_files()
            app_files["docker/docker-compose.device.yaml"] += "      - /dev/ttyUSB0:/dev/ttyUSB0\n"
            self._write_fpk(fpk, app_files)

            result = self._verify_fpk(fpk)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("device overlay", result.stderr)

    def test_verifier_rejects_a_lifecycle_script_that_calls_docker_cli(self):
        for invocation in (
            "docker inspect cardpulse",
            "sudo docker inspect cardpulse",
            "env MODE=fnos docker compose up",
            "command docker inspect cardpulse",
            "FOO=fnos docker inspect cardpulse",
            "sudo -n docker inspect cardpulse",
            "env -i docker inspect cardpulse",
            "/usr/bin/docker inspect cardpulse",
            "time docker inspect cardpulse",
            "docker-compose up",
        ):
            with self.subTest(invocation=invocation), tempfile.TemporaryDirectory() as tmpdir:
                fpk = Path(tmpdir) / "cardpulse.fpk"
                package_files = self._safe_package_files()
                package_files["cmd/main"] = f"#!/bin/sh\n{invocation}\n"
                self._write_fpk(fpk, self._safe_app_files(), package_files)

                result = self._verify_fpk(fpk)

            self.assertNotEqual(0, result.returncode)
            self.assertIn("must not invoke Docker CLI", result.stderr)

    def test_verifier_rejects_docker_cli_in_any_lifecycle_callback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            package_files = self._safe_package_files()
            package_files["cmd/install_callback"] = "#!/bin/sh\nenv MODE=fnos docker compose up\n"
            self._write_fpk(fpk, self._safe_app_files(), package_files)

            result = self._verify_fpk(fpk)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("cmd/install_callback must not invoke Docker CLI", result.stderr)

    def test_verifier_rejects_host_driver_or_udev_mutation_in_a_lifecycle_script(self):
        for command in ("udevadm control --reload-rules", "modprobe qmi_wwan"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmpdir:
                fpk = Path(tmpdir) / "cardpulse.fpk"
                package_files = self._safe_package_files()
                package_files["cmd/install_callback"] = (
                    bytes(package_files["cmd/install_callback"])
                    + f"\n{command}\n".encode("utf-8")
                )
                self._write_fpk(fpk, self._safe_app_files(), package_files)

                result = self._verify_fpk(fpk)

            self.assertNotEqual(0, result.returncode)
            self.assertIn("host mutation", result.stderr)

    def test_verifier_scans_unlisted_lifecycle_scripts_for_docker_cli(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            package_files = self._safe_package_files()
            package_files["cmd/unsupported_callback"] = "#!/bin/sh\ncommand docker inspect cardpulse\n"
            self._write_fpk(fpk, self._safe_app_files(), package_files)

            result = self._verify_fpk(fpk)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("unexpected package payload", result.stderr)

    def test_verifier_rejects_quoted_compose_safety_keys(self):
        for setting, expected in (
            ('"privileged": true', "privileged"),
            ('"ports": ["9999:9999"]', "ports"),
            ('"devices": ["/dev/random:/dev/random"]', "device"),
            ('"network_mode": "host"', "network_mode"),
        ):
            with self.subTest(setting=setting), tempfile.TemporaryDirectory() as tmpdir:
                fpk = Path(tmpdir) / "cardpulse.fpk"
                app_files = self._safe_app_files()
                app_files["docker/docker-compose.no-device.yaml"] += f"\n    {setting}\n"
                self._write_fpk(fpk, app_files)

                result = self._verify_fpk(fpk)

            self.assertNotEqual(0, result.returncode)
            self.assertIn(expected, result.stderr.lower())

    def test_verifier_rejects_compose_with_a_second_service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            app_files = self._safe_app_files()
            for compose_name in (
                "docker/docker-compose.yaml",
                "docker/docker-compose.no-device.yaml",
                "docker/docker-compose.device.yaml",
            ):
                app_files[compose_name] += f"\n  second-sender:\n    image: {self.IMAGE}\n"
            self._write_fpk(fpk, app_files)

            result = self._verify_fpk(fpk)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("exactly one cardpulse service", result.stderr)

    def test_verifier_rejects_host_networking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            app_files = self._safe_app_files()
            app_files["docker/docker-compose.no-device.yaml"] += "\n    network_mode: host\n"
            self._write_fpk(fpk, app_files)

            result = self._verify_fpk(fpk)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("network_mode: host", result.stderr)

    def test_verifier_requires_the_fixed_security_opt_and_environment_contract(self):
        mutations = (
            (
                "missing security_opt",
                lambda compose: compose.replace(
                    "\n    security_opt:\n      - no-new-privileges:true", ""
                ),
                "security_opt",
            ),
            (
                "weakened security_opt",
                lambda compose: compose.replace(
                    "no-new-privileges:true", "no-new-privileges:false"
                ),
                "security_opt",
            ),
            (
                "sms web override",
                lambda compose: compose.replace(
                    "      CARDPULSE_SCHEDULER_POLL_SECONDS: \"3600\"\n",
                    "      CARDPULSE_SCHEDULER_POLL_SECONDS: \"3600\"\n"
                    "      CARDPULSE_WEB_ALLOW_SMS: \"1\"\n",
                ),
                "environment",
            ),
        )
        for label, mutate, expected in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmpdir:
                fpk = Path(tmpdir) / "cardpulse.fpk"
                app_files = self._safe_app_files()
                app_files["docker/docker-compose.no-device.yaml"] = mutate(
                    str(app_files["docker/docker-compose.no-device.yaml"])
                )
                self._write_fpk(fpk, app_files)

                result = self._verify_fpk(fpk)

            self.assertNotEqual(0, result.returncode)
            self.assertIn(expected, result.stderr.lower())

    def test_verifier_rejects_compose_release_identity_environment_mismatches(self):
        changed_image = "registry.example/cardpulse@sha256:" + "b" * 64
        mutations = (
            (
                "runtime image",
                lambda compose: compose.replace(
                    f'CARDPULSE_RUNTIME_IMAGE: "{self.IMAGE}"',
                    f'CARDPULSE_RUNTIME_IMAGE: "{changed_image}"',
                ),
                "runtime image",
            ),
            (
                "FPK version",
                lambda compose: compose.replace(
                    'CARDPULSE_FPK_VERSION: "1.1.0"',
                    'CARDPULSE_FPK_VERSION: "1.1.1"',
                ),
                "FPK version",
            ),
        )
        for label, mutate, expected in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmpdir:
                fpk = Path(tmpdir) / "cardpulse.fpk"
                app_files = self._safe_app_files()
                app_files["docker/docker-compose.no-device.yaml"] = mutate(
                    str(app_files["docker/docker-compose.no-device.yaml"])
                )
                self._write_fpk(fpk, app_files)

                result = self._verify_fpk(fpk)

            self.assertNotEqual(0, result.returncode)
            self.assertIn(expected.lower(), result.stderr.lower())

    def test_verifier_rejects_unallowed_compose_top_level_and_service_fields(self):
        mutations = (
            ("include", "\ninclude:\n  - /tmp/untrusted-compose.yaml\n", "include"),
            ("networks", "\nnetworks:\n  hostnet: {}\n", "top-level"),
            ("labels", "\n    labels:\n      unsafe: true\n", "labels"),
            ("cap_add", "\n    cap_add:\n      - SYS_ADMIN\n", "cap_add"),
            ("pid", "\n    pid: host\n", "pid"),
            ("ipc", "\n    ipc: host\n", "ipc"),
            ("uts", "\n    uts: host\n", "uts"),
            ("command", "\n    command: /usr/local/bin/cardpulse --send\n", "command"),
            ("entrypoint", "\n    entrypoint: /bin/sh\n", "entrypoint"),
            (
                "extends",
                "\n    extends:\n      service: cardpulse\n",
                "extends",
            ),
        )
        for label, addition, expected in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmpdir:
                fpk = Path(tmpdir) / "cardpulse.fpk"
                app_files = self._safe_app_files()
                app_files["docker/docker-compose.no-device.yaml"] += addition
                self._write_fpk(fpk, app_files)

                result = self._verify_fpk(fpk)

            self.assertNotEqual(0, result.returncode)
            self.assertIn(expected, result.stderr.lower())

    def test_verifier_rejects_duplicate_compose_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            app_files = self._safe_app_files()
            app_files["docker/docker-compose.no-device.yaml"] += (
                "\n    container_name: replacement\n"
            )
            self._write_fpk(fpk, app_files)

            result = self._verify_fpk(fpk)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("duplicate", result.stderr.lower())

    def test_verifier_rejects_malformed_image_digest(self):
        malformed_image = "registry.example/cardpulse@sha256:" + "a" * 63
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            app_files = self._safe_app_files()
            for compose_name in (
                "docker/docker-compose.yaml",
                "docker/docker-compose.no-device.yaml",
                "docker/docker-compose.device.yaml",
            ):
                app_files[compose_name] = str(app_files[compose_name]).replace(
                    self.IMAGE,
                    malformed_image,
                )
            self._write_fpk(fpk, app_files)

            result = self._verify_fpk(fpk, malformed_image)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("digest", result.stderr.lower())

    def test_verifier_rejects_unexpected_compose_payload_files(self):
        mutations = (
            (
                "override",
                "docker/docker-compose.override.yaml",
                "services:\n  cardpulse:\n    privileged: true\n",
            ),
            (
                "noncanonical duplicate",
                "./docker/docker-compose.no-device.yaml",
                "services:\n  cardpulse:\n    privileged: true\n",
            ),
        )
        for label, file_name, contents in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmpdir:
                fpk = Path(tmpdir) / "cardpulse.fpk"
                app_files = self._safe_app_files()
                app_files[file_name] = contents
                self._write_fpk(fpk, app_files)

                result = self._verify_fpk(fpk)

            self.assertNotEqual(0, result.returncode)
            self.assertIn("unexpected", result.stderr.lower())

    def test_verifier_requires_fnpack_generated_config_copies_to_match_the_package(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpk = Path(tmpdir) / "cardpulse.fpk"
            app_files = self._safe_app_files()
            app_files["config/resource"] = (PACKAGE / "config" / "resource").read_bytes()
            app_files["config/privilege"] = b'{"defaults":{"run-as":"root"}}\n'
            self._write_fpk(fpk, app_files)

            result = self._verify_fpk(fpk)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("generated config copy", result.stderr)

    def test_ci_smoke_test_expects_the_sticky_gateway_socket_directory(self):
        workflow = (ROOT_DIR / ".github" / "workflows" / "fnos-package.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('test "$(stat -c %a "$smoke_dir/run")" = 3770', workflow)

    def test_ci_smoke_test_checks_the_cardpulse_version_banner(self):
        workflow = (ROOT_DIR / ".github" / "workflows" / "fnos-package.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'test "$(docker exec cardpulse-fnos-smoke /usr/local/bin/cardpulse --version)" = "CardPulse $FPK_VERSION"',
            workflow,
        )

    def test_ci_smoke_test_bootstraps_lifecycle_config_and_keeps_no_new_privileges(self):
        workflow = (ROOT_DIR / ".github" / "workflows" / "fnos-package.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('mkdir "$smoke_dir/data/config"', workflow)
        self.assertIn('chmod 2750 "$smoke_dir/data/config"', workflow)
        self.assertIn('mkdir "$smoke_dir/data/state"', workflow)
        self.assertIn('chmod 2770 "$smoke_dir/data/state"', workflow)
        self.assertIn('--security-opt no-new-privileges:true', workflow)

    def test_ci_runs_the_fpk_builder_with_an_explicit_shell(self):
        workflow = (ROOT_DIR / ".github" / "workflows" / "fnos-package.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('sh scripts/build-fnos-fpk.sh)', workflow)
        self.assertNotIn('./scripts/build-fnos-fpk.sh)', workflow)

    def test_fnos_readme_runs_the_fpk_builder_with_an_explicit_shell(self):
        readme = (ROOT_DIR / "packaging" / "fnos" / "README.md").read_text(encoding="utf-8")

        self.assertIn("  sh scripts/build-fnos-fpk.sh", readme)
        self.assertNotIn("./scripts/build-fnos-fpk.sh", readme)

    def test_ci_regresses_before_push_and_reuses_one_published_digest(self):
        workflow = (ROOT_DIR / ".github" / "workflows" / "fnos-package.yml").read_text(
            encoding="utf-8"
        )

        self.assertLess(
            workflow.index("- name: Run release regression suite"),
            workflow.index("- name: Build and publish amd64 image"),
        )
        self.assertLess(
            workflow.index("uses: mengzhuo/setup-fnpack"),
            workflow.index("- name: Build and publish amd64 image"),
        )
        self.assertIn("id: image", workflow)
        self.assertIn("containerimage.digest", workflow)
        self.assertIn('image="$IMAGE_REPOSITORY@$IMAGE_DIGEST"', workflow)
        self.assertIn('CARDPULSE_IMAGE="$IMAGE_REPOSITORY@$IMAGE_DIGEST"', workflow)
        self.assertNotIn("imagetools inspect", workflow)

    def test_ci_requires_a_preconfigured_public_ghcr_package_and_repulls_the_published_digest(self):
        workflow = (ROOT_DIR / ".github" / "workflows" / "fnos-package.yml").read_text(
            encoding="utf-8"
        )
        dockerfile = (ROOT_DIR / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("org.opencontainers.image.source", dockerfile)
        self.assertNotIn("- name: Make GitHub Container package public", workflow)
        self.assertNotIn("/user/packages/container/", workflow)
        self.assertNotIn("visibility=public", workflow)
        self.assertIn("- name: Verify anonymous GHCR pull", workflow)
        self.assertIn("docker logout ghcr.io", workflow)
        self.assertIn("set the package visibility to public", workflow)
        self.assertLess(
            workflow.index("- name: Verify anonymous GHCR pull"),
            workflow.index("- name: Run fnOS image smoke test"),
        )

    def test_docker_build_context_keeps_the_fnos_runtime_entrypoint_scripts(self):
        dockerignore = (ROOT_DIR / ".dockerignore").read_text(encoding="utf-8")

        self.assertIn("scripts/", dockerignore)
        self.assertIn("!scripts/fnos-entrypoint.sh", dockerignore)
        self.assertIn("!scripts/fnos-scheduler.sh", dockerignore)

    def test_ci_pushes_the_release_branch_with_default_release_values(self):
        workflow = (ROOT_DIR / ".github" / "workflows" / "fnos-package.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("push:\n    branches: [ 'codex/fnos-fpk-release-*' ]", workflow)
        self.assertIn(
            "IMAGE_REPOSITORY: ${{ inputs.image_repository || 'ghcr.io/henrydontbbai/cardpulse' }}",
            workflow,
        )
        self.assertIn("IMAGE_TAG: ${{ inputs.image_tag || '1.1.0-fnos' }}", workflow)
        self.assertIn("FPK_VERSION: ${{ inputs.fpk_version || '1.1.0' }}", workflow)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_uninstall_defaults_to_retain_and_deletes_only_after_explicit_choice(self):
        uninstall_init = PACKAGE / "cmd" / "uninstall_init"
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "cardpulse-data"
            data_dir.mkdir()
            audit = data_dir / "state" / "audit.jsonl"
            audit.parent.mkdir()
            audit.write_text("audit\n", encoding="utf-8")
            environment = {**os.environ, "TRIM_PKGVAR": str(data_dir)}

            retained = subprocess.run(
                ["sh", str(uninstall_init)],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(0, retained.returncode, retained.stderr)
            self.assertTrue(audit.exists())

            deleted = subprocess.run(
                ["sh", str(uninstall_init)],
                check=False,
                capture_output=True,
                text=True,
                env={**environment, "wizard_cardpulse_private_data": "delete"},
            )

            self.assertEqual(0, deleted.returncode, deleted.stderr)
            self.assertFalse(data_dir.exists())

    @unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"), "requires Unix sockets")
    def test_main_status_uses_gateway_socket_without_docker_cli_access(self):
        main = PACKAGE / "cmd" / "main"
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            app_dest = temporary_root / "appdest"
            app_dest.mkdir()
            fake_bin = temporary_root / "bin"
            fake_bin.mkdir()
            fake_docker = fake_bin / "docker"
            fake_docker.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            fake_docker.chmod(0o755)
            curl_log = temporary_root / "curl.log"
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" > \"${FAKE_CURL_LOG:?}\"\n"
                "printf '%s\\n' '{\"status\":\"ok\"}'\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            socket_path = app_dest / "app.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(socket_path))
                environment = {
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "TRIM_APPDEST": str(app_dest),
                    "FAKE_CURL_LOG": str(curl_log),
                }
                running = subprocess.run(
                    ["sh", str(main), "status"],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
            finally:
                server.close()

            self.assertEqual(0, running.returncode, running.stderr)
            curl_arguments = curl_log.read_text(encoding="utf-8")
            self.assertIn("--unix-socket", curl_arguments)
            self.assertIn(str(socket_path), curl_arguments)
            self.assertIn("X-Trim-Isadmin: true", curl_arguments)
            self.assertIn("/app/cardpulse/api/health", curl_arguments)
            socket_path.unlink()
            stopped = subprocess.run(
                ["sh", str(main), "status"],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

        self.assertEqual(3, stopped.returncode, stopped.stderr)

    def test_install_and_upgrade_call_the_legacy_scheduler_guard(self):
        guard = PACKAGE / "cmd" / "migration-guard.sh"
        self.assertTrue(guard.is_file())

        for name in ("install_init", "upgrade_init"):
            lifecycle = (PACKAGE / "cmd" / name).read_text(encoding="utf-8")
            self.assertIn("migration-guard.sh", lifecycle)

        guard_text = guard.read_text(encoding="utf-8")
        for expected in (
            "/opt/cardpulse",
            "cardpulse.timer",
            "cardpulse.service",
            "cardpulse-web.service",
            "/etc/crontab",
            "/etc/cron.d",
            "Manual migration required",
        ):
            self.assertIn(expected, guard_text)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_install_and_upgrade_refuse_an_active_legacy_timer_without_creating_app_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            fake_bin = temporary_root / "bin"
            fake_bin.mkdir()
            (fake_bin / "systemctl").write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "is-active" ] && [ "$3" = "cardpulse.timer" ]; then\n'
                "    exit 0\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            (fake_bin / "crontab").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            for command in fake_bin.iterdir():
                command.chmod(0o755)

            for lifecycle_name in ("install_init", "upgrade_init"):
                package_var = temporary_root / f"pkgvar-{lifecycle_name}"
                app_dest = temporary_root / f"appdest-{lifecycle_name}"
                environment = {
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "TRIM_PKGVAR": str(package_var),
                    "TRIM_APPDEST": str(app_dest),
                }
                completed = subprocess.run(
                    ["sh", str(PACKAGE / "cmd" / lifecycle_name)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                )

                self.assertNotEqual(0, completed.returncode)
                self.assertIn("legacy native", completed.stderr)
                self.assertIn("Manual migration required", completed.stderr)
                self.assertFalse(package_var.exists())
                self.assertFalse(app_dest.exists())

    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_upgrade_refuses_a_legacy_cron_entry_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "bin"
            fake_bin.mkdir()
            (fake_bin / "systemctl").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            (fake_bin / "crontab").write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '0 2 * * * /opt/cardpulse/bin/cardpulse # CardPulse'\n"
                "exit 0\n",
                encoding="utf-8",
            )
            for command in fake_bin.iterdir():
                command.chmod(0o755)

            completed = subprocess.run(
                ["sh", str(PACKAGE / "cmd" / "upgrade_init")],
                check=False,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "TRIM_PKGVAR": str(Path(tmpdir) / "private"),
                },
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("legacy CardPulse cron", completed.stderr)
            self.assertIn("did not modify", completed.stderr)

    def test_upgrade_only_tightens_the_lifecycle_owned_configuration_directory(self):
        upgrade = (PACKAGE / "cmd" / "upgrade_init").read_text(encoding="utf-8").lower()

        self.assertNotIn("sed", upgrade)
        self.assertIn("config-permissions.sh", upgrade)
        self.assertIn("disarm-scheduler.sh", upgrade)


if __name__ == "__main__":
    unittest.main()
