#!/usr/bin/env python3
"""Safety contract for the post-install fnOS/QDC507 read-only POC."""

import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT_DIR / "scripts" / "fnos-readonly-poc.sh"


class FnosReadonlyPocContractTest(unittest.TestCase):
    def test_poc_checks_only_the_fixed_device_gateway_and_readonly_cli(self):
        source = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("/dev/cardpulse-at:/dev/cardpulse-at", source)
        self.assertNotIn("docker exec --user cardpulse", source)
        self.assertIn("docker exec --user root", source)
        self.assertIn("setpriv --reuid=cardpulse --regid=cardpulse", source)
        self.assertIn("/var/lib/cardpulse", source)
        self.assertIn("/run/cardpulse", source)
        self.assertIn("append_gid", source)
        self.assertIn('if [ "$REQUIRE_DEVICE" = true ]; then', source)
        self.assertIn("runtime_uid=$(run_as_cardpulse id -u)", source)
        self.assertIn('"$runtime_uid" -gt 0', source)
        self.assertGreaterEqual(source.count("run_as_cardpulse /bin/sh -ceu"), 2)
        self.assertIn("cardpulse-acceptance-write", source)
        self.assertIn("cardpulse-acceptance-readback", source)
        self.assertIn(
            'MARKER_PATH="/var/lib/cardpulse/lifecycle/qdc507-readonly-acceptance.json"',
            source,
        )
        self.assertIn(
            'marker="/var/lib/cardpulse/lifecycle/qdc507-readonly-acceptance.json"',
            source,
        )
        self.assertIn('chmod 640 "$temporary_marker"', source)
        self.assertIn('test -r "$marker"', source)
        self.assertIn('test ! -w "$marker"', source)
        self.assertIn("2ca3:4006", source)
        self.assertIn("/sys/class/tty", source)
        self.assertIn("resolved_device", source)
        self.assertIn("runtime_version", source)
        self.assertIn('"schema_version":3', source)
        self.assertIn("image_reference", source)
        self.assertIn("package_version", source)
        self.assertIn("CARDPULSE_RUNTIME_IMAGE", source)
        self.assertIn("CARDPULSE_FPK_VERSION", source)
        self.assertIn("socket_backend", source)
        self.assertNotIn('\\\"gateway\\\":true', source)
        self.assertIn("--unix-socket", source)
        self.assertIn("X-Trim-Isadmin: true", source)
        self.assertIn("HostConfig.Privileged", source)
        self.assertIn("HostConfig.NetworkMode", source)
        self.assertIn("HostConfig.CapAdd", source)
        self.assertIn("HostConfig.SecurityOpt", source)
        self.assertIn("container must not add Linux capabilities", source)
        self.assertIn("no-new-privileges:true", source)
        self.assertIn("range .Mounts", source)
        self.assertIn("unexpected host mount destination", source)
        self.assertIn("runtime cardpulse user can modify package assets", source)
        self.assertIn("cardpulse-package-assets", source)
        for package_asset in (
            "/run/cardpulse/docker/docker-compose.yaml",
            "/run/cardpulse/ui/config",
            "/run/cardpulse/diagnostics/fnos-readonly-poc.sh",
        ):
            self.assertIn(package_asset, source)
        self.assertIn("(^|:)/dev(/|$)", source)
        self.assertIn("--require-device", source)
        self.assertIn("--record-acceptance", source)
        self.assertIn("qdc507-readonly-acceptance.json", source)
        for command in ("--doctor", "--info", "--sms-status", "--status"):
            self.assertIn(command, source)
        for prohibited in ("--test", "--delete-sms", "--reset", "docker compose", "docker restart"):
            self.assertNotIn(prohibited, source)

    @unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"), "requires POSIX Unix sockets")
    def test_poc_runs_diagnostics_with_the_runtime_device_group_without_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            fake_bin = temporary_root / "bin"
            fake_bin.mkdir()
            log_path = temporary_root / "docker.log"
            socket_path = temporary_root / "app.sock"

            commands = {
                "docker": """#!/bin/sh
set -eu
log_path=${FAKE_DOCKER_LOG:?}
case "$1" in
inspect)
    shift
    if [ "${1:-}" = "--format" ]; then
        template=$2
        case "$template" in
            *HostConfig.Privileged*) printf '%s\\n' false ;;
            *HostConfig.NetworkMode*) printf '%s\\n' bridge ;;
            *HostConfig.CapAdd*) printf '%s\\n' '[]' ;;
            *HostConfig.SecurityOpt*) printf '%s\\n' '["no-new-privileges:true"]' ;;
            *.Source*) printf '%s\\n' '/tmp/cardpulse-data:/var/lib/cardpulse' '/tmp/cardpulse-run:/run/cardpulse' ;;
            *Mounts*) printf '%s\\n' /var/lib/cardpulse /run/cardpulse ;;
            *HostConfig.Devices*) printf '%s\\n' '/dev/cardpulse-at:/dev/cardpulse-at:rwm' ;;
            *PortBindings*) printf '%s\\n' '{}' ;;
            *) exit 91 ;;
        esac
    fi
    ;;
exec)
    shift
    user=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --user) user=$2; shift 2 ;;
            -e) shift 2 ;;
            *) break ;;
        esac
    done
    [ "${1:-}" = "cardpulse" ] || exit 92
    shift
    printf 'docker-exec user=%s command=%s\\n' "$user" "$1" >> "$log_path"
    if [ "$user" = root ]; then
        case "$*" in
            *cardpulse-package-assets*) exit 0 ;;
        esac
        exec "$@"
    fi
    exit 0
    ;;
*) exit 93 ;;
esac
""",
                "curl": """#!/bin/sh
set -eu
printf 'curl %s\\n' "$*" >> "${FAKE_DOCKER_LOG:?}"
case "$*" in
    *"X-Trim-Isadmin: true"*) printf '%s\\n' '{"status":"ok"}' ;;
    *) exit 22 ;;
esac
""",
                "stat": """#!/bin/sh
set -eu
printf 'stat %s\\n' "$*" >> "${FAKE_DOCKER_LOG:?}"
case "${3:-}" in
    /var/lib/cardpulse) printf '%s\\n' 2456 ;;
    /run/cardpulse) printf '%s\\n' 3456 ;;
    /dev/cardpulse-at) printf '%s\\n' 2456 ;;
    *) exit 96 ;;
esac
""",
                "id": """#!/bin/sh
case "${1:-}" in
-g) printf '%s\\n' 1000 ;;
-u) printf '%s\\n' 1001 ;;
*) exit 94 ;;
esac
""",
                "setpriv": """#!/bin/sh
set -eu
log_path=${FAKE_DOCKER_LOG:?}
uid=""
groups=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --reuid=*) uid=${1#--reuid=} ;;
        --regid=*|--nnp) : ;;
        --groups=*) groups=${1#--groups=} ;;
        *) break ;;
    esac
    shift
done
printf 'setpriv uid=%s groups=%s command=%s\\n' "$uid" "$groups" "$1" >> "$log_path"
case "$1" in
id) printf '%s\\n' 1001 ;;
test) exit 0 ;;
/bin/sh) exit 0 ;;
/usr/local/bin/cardpulse)
    printf 'cardpulse %s\\n' "$2" >> "$log_path"
    ;;
*) exit 95 ;;
esac
""",
            }
            for name, content in commands.items():
                command_path = fake_bin / name
                command_path.write_text(content, encoding="utf-8")
                command_path.chmod(0o755)

            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(socket_path))
                result = subprocess.run(
                    [
                        "sh",
                        str(SCRIPT_PATH),
                        "--socket",
                        str(socket_path),
                        "--container",
                        "cardpulse",
                        "--require-device",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                        "FAKE_DOCKER_LOG": str(log_path),
                    },
                )
            finally:
                server.close()

            self.assertEqual(0, result.returncode, result.stderr)
            log = log_path.read_text(encoding="utf-8")
        self.assertIn("setpriv uid=cardpulse groups=1000,2456,3456 command=id", log)
        self.assertIn("setpriv uid=cardpulse groups=1000,2456,3456 command=/bin/sh", log)
        curl_lines = [line for line in log.splitlines() if line.startswith("curl ")]
        self.assertEqual(2, len(curl_lines))
        self.assertNotIn("X-Trim-Isadmin", curl_lines[0])
        self.assertIn("X-Trim-Isadmin: true", curl_lines[1])
        for command in ("--doctor", "--info", "--sms-status", "--status"):
            self.assertIn(f"cardpulse {command}", log)


if __name__ == "__main__":
    unittest.main()
