#!/usr/bin/env python3
"""Static safety contract for the no-device fnOS platform POC.

The real gateway authorization boundary is deliberately not emulated here;
that requires the fnOS HTTPS gateway and separate administrator/user sessions
on NAS 56.
"""

from pathlib import Path
import unittest


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT_DIR / "scripts" / "fnos-platform-poc.sh"


class FnosPlatformPocContractTest(unittest.TestCase):
    def test_platform_poc_checks_no_device_runtime_without_claiming_gateway_auth(self):
        self.assertTrue(SCRIPT_PATH.is_file(), "missing fnOS no-device platform POC")
        source = SCRIPT_PATH.read_text(encoding="utf-8")

        for required in (
            "--socket",
            "--container",
            "--record-persistence",
            "TRIM_PKGVAR",
            "TRIM_APPDEST",
            "HostConfig.Privileged",
            "HostConfig.NetworkMode",
            "HostConfig.CapAdd",
            "HostConfig.SecurityOpt",
            "HostConfig.Devices",
            "PortBindings",
            "no-new-privileges:true",
            "/var/lib/cardpulse",
            "/run/cardpulse",
            'stat -c "%a" /run/cardpulse',
            "cardpulse_web.py",
            "fnos-scheduler.sh",
            "fnos-platform-poc.json",
            "cardpulse-platform-package-assets",
            "X-Trim-Isadmin: true",
            "do not validate the fnOS HTTPS gateway",
        ):
            self.assertIn(required, source)

        for prohibited in (
            "/dev/cardpulse-at",
            "/usr/local/bin/cardpulse --doctor",
            "/usr/local/bin/cardpulse --info",
            "/usr/local/bin/cardpulse --sms-status",
            "/usr/local/bin/cardpulse --status",
            "--test",
            "--delete-sms",
            "docker compose",
            "docker restart",
            "privileged: true",
        ):
            self.assertNotIn(prohibited, source)


if __name__ == "__main__":
    unittest.main()
