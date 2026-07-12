#!/usr/bin/env python3
"""Runtime contract for fnOS dynamic supplementary GID handling."""

import os
from pathlib import Path
import socket
import subprocess
import tempfile
import textwrap
import unittest


ROOT_DIR = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT_DIR / "scripts" / "fnos-entrypoint.sh"
PLATFORM_POC = ROOT_DIR / "scripts" / "fnos-platform-poc.sh"
READONLY_POC = ROOT_DIR / "scripts" / "fnos-readonly-poc.sh"


@unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"), "requires POSIX shell and Unix sockets")
class FnosDynamicGidContractTest(unittest.TestCase):
    """Exercise the same nested shell path used by the runtime and both POCs."""

    def _write_executable(self, path: Path, source: str) -> None:
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        path.chmod(0o755)

    def _write_fake_commands(self, temporary_root: Path) -> Path:
        fake_bin = temporary_root / "bin"
        fake_bin.mkdir()

        commands = {
            "id": """
                #!/bin/sh
                case "${1:-}" in
                    -u) printf '%s\\n' 0 ;;
                    -g) printf '%s\\n' 1000 ;;
                    *) exit 91 ;;
                esac
            """,
            "stat": """
                #!/bin/sh
                set -eu
                [ "${1:-}" = -c ] || exit 92
                case "${2:-}" in
                    %g) printf '%s\\n' "${FAKE_GID-}" ;;
                    %u) printf '%s\\n' 1000 ;;
                    %a)
                        case "${3:-}" in
                            */config) printf '%s\\n' 2750 ;;
                            *) printf '%s\\n' 3770 ;;
                        esac
                        ;;
                    *) exit 92 ;;
                esac
            """,
            "getent": """
                #!/bin/sh
                set -eu
                [ "${1:-}" = group ] || exit 93
                if [ "${FAKE_GROUP_ENTRY+x}" = x ]; then
                    printf '%s\\n' "$FAKE_GROUP_ENTRY"
                    exit 0
                fi
                exit 2
            """,
            "mkdir": """
                #!/bin/sh
                exit 0
            """,
            "chmod": """
                #!/bin/sh
                exit 0
            """,
            "chgrp": """
                #!/bin/sh
                exit 0
            """,
            "setpriv": """
                #!/bin/sh
                set -eu
                log_path=${FAKE_GID_LOG:?}
                groups=""
                while [ "$#" -gt 0 ]; do
                    case "$1" in
                        --groups=*) groups=${1#--groups=} ;;
                        --reuid=*|--regid=*|--nnp) : ;;
                        *) break ;;
                    esac
                    shift
                done
                printf 'setpriv groups=%s command=%s\\n' "$groups" "${1:-}" >> "$log_path"
                if [ "${1:-}" = id ]; then
                    printf '%s\\n' 1001
                fi
            """,
            "docker": """
                #!/bin/sh
                set -eu
                case "${1:-}" in
                    inspect)
                        shift
                        if [ "${1:-}" != --format ]; then
                            exit 0
                        fi
                        template=$2
                        case "$template" in
                            *HostConfig.Privileged*) printf '%s\\n' false ;;
                            *HostConfig.NetworkMode*) printf '%s\\n' bridge ;;
                            *HostConfig.CapAdd*) printf '%s\\n' '[]' ;;
                            *HostConfig.SecurityOpt*) printf '%s\\n' '["no-new-privileges:true"]' ;;
                            *HostConfig.Devices*)
                                if [ "${FAKE_DEVICE_MAPPING:-false}" = true ]; then
                                    printf '%s\\n' '/dev/cardpulse-at:/dev/cardpulse-at:rwm'
                                fi
                                ;;
                            *PortBindings*) printf '%s\\n' '{}' ;;
                            *.Source*)
                                printf '%s:%s\\n' "${TRIM_PKGVAR:-/tmp/cardpulse-data}" /var/lib/cardpulse
                                printf '%s:%s\\n' "${TRIM_APPDEST:-/tmp/cardpulse-run}" /run/cardpulse
                                ;;
                            *.Destination*)
                                printf '%s\\n' /var/lib/cardpulse /run/cardpulse
                                ;;
                            *Config.Env*) printf '%s\\n' 'CARDPULSE_RUNTIME_VERSION=1.1.0' ;;
                            *) exit 94 ;;
                        esac
                        ;;
                    exec)
                        shift
                        while [ "$#" -gt 0 ]; do
                            case "$1" in
                                --user) shift 2 ;;
                                -e) shift 2 ;;
                                *) break ;;
                            esac
                        done
                        [ "${1:-}" = cardpulse ] || exit 95
                        shift
                        case "$*" in
                            *cardpulse-package-assets*|*cardpulse-platform-package-assets*|*cardpulse-platform-process*) exit 0 ;;
                        esac
                        exec "$@"
                        ;;
                    *) exit 96 ;;
                esac
            """,
            "curl": """
                #!/bin/sh
                set -eu
                case "$*" in
                    *"X-Trim-Isadmin: true"*) printf '%s\\n' '{"status":"ok"}' ;;
                    *) exit 22 ;;
                esac
            """,
        }
        for name, source in commands.items():
            self._write_executable(fake_bin / name, source)
        return fake_bin

    def _run_runtime_path(
        self,
        script: Path,
        gid: str,
        group_entry: str | None,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            fake_bin = self._write_fake_commands(temporary_root)
            log_path = temporary_root / "setpriv.log"
            socket_path = temporary_root / "app.sock"
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "FAKE_GID": gid,
                "FAKE_GID_LOG": str(log_path),
            }
            if group_entry is not None:
                environment["FAKE_GROUP_ENTRY"] = group_entry
            else:
                environment.pop("FAKE_GROUP_ENTRY", None)

            if script == ENTRYPOINT:
                device_path = temporary_root / "dev" / "cardpulse-at"
                device_path.parent.mkdir()
                device_path.touch()
                data_dir = temporary_root / "data"
                config_dir = data_dir / "config"
                config_dir.mkdir(parents=True)
                (config_dir / "config.yaml").write_text("scheduler:\n  enabled: false\n", encoding="utf-8")
                entrypoint_copy = temporary_root / "fnos-entrypoint.sh"
                entrypoint_copy.write_text(
                    ENTRYPOINT.read_text(encoding="utf-8").replace("/dev/cardpulse-at", str(device_path)),
                    encoding="utf-8",
                )
                entrypoint_copy.chmod(0o755)
                environment.update(
                    {
                        "CARDPULSE_DATA_DIR": str(data_dir),
                        "CARDPULSE_WEB_SOCKET": str(temporary_root / "run" / "app.sock"),
                    }
                )
                result = subprocess.run(
                    ["bash", str(entrypoint_copy), "--version"],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
            else:
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    server.bind(str(socket_path))
                    command = ["sh", str(script), "--socket", str(socket_path), "--container", "cardpulse"]
                    if script == PLATFORM_POC:
                        environment.update(
                            {
                                "TRIM_PKGVAR": "/tmp/cardpulse-data",
                                "TRIM_APPDEST": "/tmp/cardpulse-run",
                                "FAKE_DEVICE_MAPPING": "false",
                            }
                        )
                    else:
                        command.append("--require-device")
                        environment["FAKE_DEVICE_MAPPING"] = "true"
                    result = subprocess.run(
                        command,
                        check=False,
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                finally:
                    server.close()

            log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            return result, log

    def test_runtime_paths_reject_unsafe_dynamic_gids_before_setpriv(self):
        unsafe_cases = (
            ("missing", "", None),
            ("non-numeric", "not-a-gid", None),
            ("root", "0", None),
            ("sensitive-image-group", "1", "daemon:x:1:"),
        )
        for script in (ENTRYPOINT, PLATFORM_POC, READONLY_POC):
            for case_name, gid, group_entry in unsafe_cases:
                with self.subTest(script=script.name, case=case_name):
                    result, log = self._run_runtime_path(script, gid, group_entry)

                    self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertIn("unsafe dynamic GID", result.stderr)
                    self.assertNotIn("setpriv", log)

    def test_runtime_paths_allow_unknown_host_gid_and_dialout(self):
        allowed_cases = (
            ("unknown-host-group", "4567", None),
            ("dialout", "20", "dialout:x:20:cardpulse"),
        )
        for script in (ENTRYPOINT, PLATFORM_POC, READONLY_POC):
            for case_name, gid, group_entry in allowed_cases:
                with self.subTest(script=script.name, case=case_name):
                    result, log = self._run_runtime_path(script, gid, group_entry)

                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn(f"groups=1000,{gid}", log)


if __name__ == "__main__":
    unittest.main()
