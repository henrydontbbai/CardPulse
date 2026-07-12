#!/usr/bin/env python3
"""Verify the release-critical contents of a fnpack-generated CardPulse FPK."""

from __future__ import annotations

import argparse
import configparser
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile

import yaml


IMAGE_DIGEST_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-fA-F]{64}$")
RELEASE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
HOST_MUTATION_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:udevadm|modprobe|insmod|rmmod|depmod)(?=[ \t;|&()#\r\n]|$)"
)
SOURCE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PACKAGE_DIR = SOURCE_ROOT / "packaging" / "fnos" / "cardpulse"
SOURCE_READONLY_POC = Path(__file__).with_name("fnos-readonly-poc.sh")
SOURCE_PLATFORM_POC = Path(__file__).with_name("fnos-platform-poc.sh")


class DuplicateComposeKeyError(ValueError):
    """Raised when a Compose mapping would have parser-dependent duplicate keys."""


class StrictComposeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate and merged Compose mappings."""


def construct_unique_compose_mapping(
    loader: StrictComposeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise DuplicateComposeKeyError("YAML merge keys are not allowed")
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise DuplicateComposeKeyError("YAML mapping keys must be scalar values") from exc
        if duplicate:
            raise DuplicateComposeKeyError(f"duplicate YAML mapping key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictComposeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    construct_unique_compose_mapping,
)


def validated_archive_members(
    archive: tarfile.TarFile,
    label: str,
) -> dict[str, tarfile.TarInfo]:
    """Reject archive names whose extraction semantics could differ by consumer."""
    members: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        raw_name = member.name
        parts = raw_name.split("/")
        if (
            not raw_name
            or raw_name.startswith("/")
            or "\\" in raw_name
            or any(part in {".", ".."} for part in parts)
            or any(not part for part in parts[:-1])
        ):
            raise ValueError(f"{label} contains an unexpected archive path: {raw_name!r}")

        normalized_name = str(PurePosixPath(raw_name)).rstrip("/")
        if not normalized_name:
            raise ValueError(f"{label} contains an unexpected archive path: {raw_name!r}")
        if normalized_name in members:
            raise ValueError(f"{label} contains an unexpected duplicate archive path: {normalized_name}")
        if not (member.isfile() or member.isdir()) or member.issym() or member.islnk():
            raise ValueError(f"{label} contains an unexpected non-file payload: {normalized_name}")
        members[normalized_name] = member
    return members


REQUIRED_FILES = {
    "manifest",
    "ICON.PNG",
    "ICON_256.PNG",
    "app.tgz",
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
}

REQUIRED_PACKAGE_TEXT_FILES = {
    "manifest",
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
}

REQUIRED_APP_FILES = {
    "config/privilege",
    "config/resource",
    "docker/docker-compose.yaml",
    "docker/docker-compose.no-device.yaml",
    "docker/docker-compose.device.yaml",
    "ui/config",
    "ui/images/icon_64.png",
    "ui/images/icon_256.png",
    "diagnostics/fnos-readonly-poc.sh",
    "diagnostics/fnos-platform-poc.sh",
}

REQUIRED_COMPOSE_VOLUMES = [
    "${TRIM_PKGVAR}:/var/lib/cardpulse",
    "${TRIM_APPDEST}:/run/cardpulse",
]

REQUIRED_COMPOSE_ENVIRONMENT = {
    "CARDPULSE_DATA_DIR": "/var/lib/cardpulse",
    "CARDPULSE_WEB_SOCKET": "/run/cardpulse/app.sock",
    "CARDPULSE_WEB_BASE_PATH": "/app/cardpulse",
    "CARDPULSE_FNOS_RUNTIME": "1",
    "CARDPULSE_SCHEDULER_POLL_SECONDS": "3600",
}

REQUIRED_COMPOSE_SECURITY_OPT = ["no-new-privileges:true"]

ALLOWED_COMPOSE_TOP_LEVEL_KEYS = frozenset({"services"})
ALLOWED_COMPOSE_SERVICE_KEYS = frozenset(
    {
        "image",
        "container_name",
        "restart",
        "environment",
        "volumes",
        "devices",
        "security_opt",
    }
)
PROHIBITED_COMPOSE_TOP_LEVEL_KEYS = frozenset({"include", "extends"})
PROHIBITED_COMPOSE_SERVICE_KEYS = frozenset(
    {
        "privileged",
        "ports",
        "device_cgroup_rules",
        "cap_add",
        "pid",
        "ipc",
        "uts",
        "command",
        "entrypoint",
        "extends",
    }
)

READONLY_POC_REQUIRED_TEXT = (
    "/dev/cardpulse-at:/dev/cardpulse-at",
    "docker exec --user root",
    "setpriv --reuid=cardpulse --regid=cardpulse",
    "append_gid",
    "for runtime_path in /var/lib/cardpulse /run/cardpulse /dev/cardpulse-at",
    "run_as_cardpulse /bin/sh -ceu",
    "cardpulse-acceptance-write",
    "cardpulse-acceptance-readback",
    'MARKER_PATH="/var/lib/cardpulse/lifecycle/qdc507-readonly-acceptance.json"',
    'marker="/var/lib/cardpulse/lifecycle/qdc507-readonly-acceptance.json"',
    'chmod 640 "$temporary_marker"',
    'test ! -w "$marker"',
    "runtime_uid=$(run_as_cardpulse id -u)",
    '"$runtime_uid" -gt 0',
    '"schema_version":3',
    "image_reference",
    "package_version",
    "CARDPULSE_RUNTIME_IMAGE",
    "CARDPULSE_FPK_VERSION",
    "--unix-socket",
    "X-Trim-Isadmin: true",
    "HostConfig.Privileged",
    "HostConfig.NetworkMode",
    "range .Mounts",
    "(^|:)/dev(/|$)",
    "--require-device",
    "--record-acceptance",
    "qdc507-readonly-acceptance.json",
    "--doctor",
    "--info",
    "--sms-status",
    "--status",
    "PortBindings",
    "for command in --doctor --info --sms-status --status; do",
    'run_as_cardpulse /usr/local/bin/cardpulse "$command"',
)

READONLY_POC_PROHIBITED_TEXT = (
    "--test",
    "--delete-sms",
    "--reset",
    "docker exec --user cardpulse",
    "docker compose",
    "docker restart",
)

PLATFORM_POC_REQUIRED_TEXT = (
    "TRIM_PKGVAR",
    "TRIM_APPDEST",
    "HostConfig.Privileged",
    "HostConfig.NetworkMode",
    "HostConfig.CapAdd",
    "HostConfig.SecurityOpt",
    "HostConfig.Devices",
    "PortBindings",
    'stat -c "%a" /run/cardpulse',
    "cardpulse_web.py",
    "fnos-scheduler.sh",
    "fnos-platform-poc.json",
    "do not validate the fnOS HTTPS gateway",
)

PLATFORM_POC_PROHIBITED_TEXT = (
    "/dev/cardpulse-at",
    "/usr/local/bin/cardpulse --doctor",
    "/usr/local/bin/cardpulse --info",
    "/usr/local/bin/cardpulse --sms-status",
    "/usr/local/bin/cardpulse --status",
    "--test",
    "--delete-sms",
    "docker compose",
    "docker restart",
)

DOCKER_CLI_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.-])docker(?:-compose)?(?=[ \t;|&()#\r\n]|$)"
)


def parse_compose_service(compose: str, compose_name: str) -> dict[str, object]:
    try:
        parsed = yaml.load(compose, Loader=StrictComposeLoader)
    except DuplicateComposeKeyError as exc:
        raise ValueError(f"FPK {compose_name} contains {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"FPK {compose_name} is not valid YAML") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"FPK {compose_name} must be a YAML mapping")
    unexpected_top_level_keys = set(parsed) - ALLOWED_COMPOSE_TOP_LEVEL_KEYS
    if unexpected_top_level_keys:
        prohibited_references = sorted(
            str(key)
            for key in unexpected_top_level_keys
            if key in PROHIBITED_COMPOSE_TOP_LEVEL_KEYS
        )
        if prohibited_references:
            raise ValueError(
                f"FPK {compose_name} contains prohibited Compose reference: "
                + ", ".join(prohibited_references)
            )
        raise ValueError(
            f"FPK {compose_name} contains unsupported top-level setting: "
            + ", ".join(sorted(str(key) for key in unexpected_top_level_keys))
        )
    services = parsed.get("services")
    if not isinstance(services, dict) or set(services) != {"cardpulse"}:
        raise ValueError(f"FPK {compose_name} must declare exactly one cardpulse service")
    service = services["cardpulse"]
    if not isinstance(service, dict):
        raise ValueError(f"FPK {compose_name} cardpulse service is invalid")
    return service


def validate_compose_image(service: dict[str, object], compose_name: str, image: str) -> None:
    if service.get("image") != image:
        raise ValueError(f"FPK {compose_name} image does not match the requested digest")
    if not IMAGE_DIGEST_REFERENCE.fullmatch(image):
        raise ValueError(f"FPK {compose_name} image is not pinned by an exact sha256 digest")


def required_compose_environment(image: str, version: str) -> dict[str, str]:
    return {
        **REQUIRED_COMPOSE_ENVIRONMENT,
        "CARDPULSE_RUNTIME_IMAGE": image,
        "CARDPULSE_FPK_VERSION": version,
    }


def validate_compose_safety(
    service: dict[str, object],
    compose_name: str,
    image: str,
    version: str,
) -> None:
    if service.get("container_name") != "cardpulse":
        raise ValueError(f"FPK {compose_name} must set container_name to cardpulse")
    if service.get("restart") != "unless-stopped":
        raise ValueError(f"FPK {compose_name} must set restart to unless-stopped")
    if "network_mode" in service:
        raise ValueError(
            f"FPK {compose_name} contains prohibited setting: network_mode: {service['network_mode']}"
        )
    prohibited_service_keys = sorted(
        str(key) for key in set(service) & PROHIBITED_COMPOSE_SERVICE_KEYS
    )
    if prohibited_service_keys:
        raise ValueError(
            f"FPK {compose_name} contains prohibited setting: "
            + ", ".join(prohibited_service_keys)
        )
    unexpected_service_keys = set(service) - ALLOWED_COMPOSE_SERVICE_KEYS
    if unexpected_service_keys:
        raise ValueError(
            f"FPK {compose_name} contains unsupported service setting: "
            + ", ".join(sorted(str(key) for key in unexpected_service_keys))
        )
    environment = service.get("environment")
    expected_environment = required_compose_environment(image, version)
    if environment != expected_environment:
        if not isinstance(environment, dict):
            raise ValueError(
                f"FPK {compose_name} environment must exactly match the fnOS runtime contract"
            )
        if environment.get("CARDPULSE_RUNTIME_IMAGE") != image:
            raise ValueError(
                f"FPK {compose_name} runtime image environment does not match the requested digest"
            )
        if environment.get("CARDPULSE_FPK_VERSION") != version:
            raise ValueError(
                f"FPK {compose_name} FPK version environment does not match the requested release version"
            )
        raise ValueError(
            f"FPK {compose_name} environment must exactly match the fnOS runtime contract"
        )
    if service.get("volumes") != REQUIRED_COMPOSE_VOLUMES:
        raise ValueError(f"FPK {compose_name} must mount only the private data and gateway socket paths")
    if service.get("security_opt") != REQUIRED_COMPOSE_SECURITY_OPT:
        raise ValueError(
            f"FPK {compose_name} security_opt must contain only no-new-privileges:true"
        )


def validate_no_device_compose(service: dict[str, object], compose_name: str) -> None:
    if "devices" in service:
        raise ValueError(f"FPK {compose_name} must not declare device mappings")


def validate_device_compose(service: dict[str, object]) -> None:
    if service.get("devices") != ["/dev/cardpulse-at:/dev/cardpulse-at"]:
        raise ValueError("FPK device overlay must map only /dev/cardpulse-at:/dev/cardpulse-at")


def validate_readonly_poc(source: str) -> None:
    for required in READONLY_POC_REQUIRED_TEXT:
        if required not in source:
            raise ValueError(f"FPK diagnostics/fnos-readonly-poc.sh is missing: {required}")
    for prohibited in READONLY_POC_PROHIBITED_TEXT:
        if prohibited in source:
            raise ValueError(f"FPK diagnostics/fnos-readonly-poc.sh contains prohibited action: {prohibited}")
    if source.count("/usr/local/bin/cardpulse") != 1:
        raise ValueError("FPK diagnostics/fnos-readonly-poc.sh must run only the read-only command loop")
    try:
        reviewed_source = SOURCE_READONLY_POC.read_bytes().decode("utf-8")
    except OSError as exc:
        raise ValueError("cannot load the reviewed POC source") from exc
    if source != reviewed_source:
        raise ValueError("FPK diagnostics/fnos-readonly-poc.sh does not match the reviewed POC source")


def validate_platform_poc(source: str) -> None:
    for required in PLATFORM_POC_REQUIRED_TEXT:
        if required not in source:
            raise ValueError(f"FPK diagnostics/fnos-platform-poc.sh is missing: {required}")
    for prohibited in PLATFORM_POC_PROHIBITED_TEXT:
        if prohibited in source:
            raise ValueError(f"FPK diagnostics/fnos-platform-poc.sh contains prohibited action: {prohibited}")
    try:
        reviewed_source = SOURCE_PLATFORM_POC.read_bytes().decode("utf-8")
    except OSError as exc:
        raise ValueError("cannot load the reviewed platform POC source") from exc
    if source != reviewed_source:
        raise ValueError("FPK diagnostics/fnos-platform-poc.sh does not match the reviewed POC source")


def validate_gateway_launcher(ui_config: dict[str, object]) -> None:
    try:
        launcher = ui_config[".url"]["cardpulse.Application"]
    except (KeyError, TypeError) as exc:
        raise ValueError("FPK UI launcher is missing") from exc
    if not isinstance(launcher, dict):
        raise ValueError("FPK UI launcher is invalid")
    expected = {
        "type": "iframe",
        "protocol": "",
        "gatewayPrefix": "/app/cardpulse",
        "gatewaySocket": "app.sock",
        "url": "/app/cardpulse",
        "allUsers": False,
    }
    for key, value in expected.items():
        if launcher.get(key) != value:
            raise ValueError(f"FPK UI launcher must set {key} to {value!r}")


def load_json(source: str, label: str) -> object:
    try:
        return json.loads(source)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FPK {label} must contain valid JSON") from exc


def wizard_field(source: str, field_name: str, label: str) -> dict[str, object]:
    wizard = load_json(source, label)
    if not isinstance(wizard, list):
        raise ValueError(f"FPK {label} must be a JSON array")
    for step in wizard:
        if not isinstance(step, dict):
            continue
        items = step.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("field") == field_name:
                return item
    raise ValueError(f"FPK {label} is missing {field_name}")


def release_version(value: str) -> str:
    if not RELEASE_VERSION.fullmatch(value):
        raise argparse.ArgumentTypeError("release version must use X.Y.Z form")
    return value


def validate_manifest(package: configparser.SectionProxy, expected_version: str) -> None:
    if package.get("appname") != "cardpulse":
        raise ValueError("FPK manifest appname must be cardpulse")
    if package.get("version") != expected_version or package.get("platform") != "x86":
        raise ValueError("FPK manifest version/platform does not match the release contract")
    if package.get("disable_authorization_path") != "true":
        raise ValueError("FPK manifest must disable irrelevant directory authorization")


def validate_resource(source: str) -> None:
    expected = {
        "docker-project": {
            "projects": [
                {
                    "name": "cardpulse",
                    "path": "docker",
                }
            ]
        }
    }
    if load_json(source, "config/resource") != expected:
        raise ValueError("FPK resource must declare only the CardPulse Docker project")


def validate_privilege(source: str) -> None:
    expected = {
        "defaults": {"run-as": "package"},
        "username": "docker-cardpulse",
        "groupname": "docker-cardpulse",
    }
    if load_json(source, "config/privilege") != expected:
        raise ValueError("FPK privilege must use only the dedicated package user")


def validate_device_wizard(source: str, label: str) -> None:
    item = wizard_field(source, "wizard_qdc507_device_mode", label)
    if item.get("type") != "select" or item.get("initValue") != "disabled":
        raise ValueError(f"FPK {label} must default QDC507 device mode to disabled")
    options = item.get("options")
    values = {
        option.get("value")
        for option in options
        if isinstance(option, dict)
    } if isinstance(options, list) else set()
    if values != {"disabled", "enabled"}:
        raise ValueError(f"FPK {label} must expose only the fixed QDC507 device mode choices")


def validate_uninstall_wizard(source: str) -> None:
    item = wizard_field(source, "wizard_cardpulse_private_data", "wizard/uninstall")
    if item.get("type") != "select" or item.get("initValue") != "retain":
        raise ValueError("FPK wizard/uninstall must default private data to retain")
    options = item.get("options")
    values = {
        option.get("value")
        for option in options
        if isinstance(option, dict)
    } if isinstance(options, list) else set()
    if values != {"retain", "delete"}:
        raise ValueError("FPK wizard/uninstall must expose retain and delete choices")


def validate_lifecycle_scripts(scripts: dict[str, str]) -> None:
    for script_name, source in scripts.items():
        if script_name.startswith("cmd/") and DOCKER_CLI_TOKEN.search(source):
            raise ValueError(f"FPK {script_name} must not invoke Docker CLI")
        if script_name.startswith("cmd/") and HOST_MUTATION_TOKEN.search(source):
            raise ValueError(f"FPK {script_name} contains prohibited host mutation")
        if script_name.startswith("cmd/"):
            try:
                reviewed_source = (SOURCE_PACKAGE_DIR / script_name).read_bytes().decode("utf-8")
            except OSError as exc:
                raise ValueError(f"cannot load reviewed lifecycle source: {script_name}") from exc
            if source != reviewed_source:
                raise ValueError(f"FPK {script_name} does not match reviewed lifecycle source")

    main = scripts["cmd/main"]
    for required in (
        "start)",
        "select_cardpulse_device_mode",
        "stop)",
        "status)",
        'if [ -S "${TRIM_APPDEST}/app.sock" ]; then',
        "--unix-socket",
        'X-Trim-Isadmin: true',
        "/app/cardpulse/api/health",
        "exit 3",
    ):
        if required not in main:
            raise ValueError(f"FPK cmd/main is missing required lifecycle behavior: {required}")

    if "migration-guard.sh" not in scripts["cmd/install_init"]:
        raise ValueError("FPK cmd/install_init must run the legacy scheduler guard")
    if "config-permissions.sh" not in scripts["cmd/install_init"]:
        raise ValueError("FPK cmd/install_init must validate the lifecycle configuration directory")
    if "migration-guard.sh" not in scripts["cmd/upgrade_init"]:
        raise ValueError("FPK cmd/upgrade_init must run the legacy scheduler guard")
    if "config-permissions.sh" not in scripts["cmd/upgrade_init"]:
        raise ValueError("FPK cmd/upgrade_init must validate the lifecycle configuration directory")
    if "disarm-scheduler.sh" not in scripts["cmd/upgrade_init"]:
        raise ValueError("FPK cmd/upgrade_init must disarm the persisted scheduler")
    if "configure_cardpulse_device_mode_from_wizard" not in scripts["cmd/install_callback"]:
        raise ValueError("FPK cmd/install_callback must apply the selected device mode")
    if "select_cardpulse_device_mode" not in scripts["cmd/upgrade_callback"]:
        raise ValueError("FPK cmd/upgrade_callback must re-check the selected device mode")
    if "configure_cardpulse_device_mode_from_wizard" not in scripts["cmd/config_callback"]:
        raise ValueError("FPK cmd/config_callback must apply the selected device mode")

    uninstall_init = scripts["cmd/uninstall_init"]
    for required in (
        "wizard_cardpulse_private_data",
        "retain)",
        "delete)",
        "-mindepth 1 -depth -delete",
    ):
        if required not in uninstall_init:
            raise ValueError(f"FPK cmd/uninstall_init is missing private-data behavior: {required}")
    if "exit 0" not in scripts["cmd/uninstall_callback"]:
        raise ValueError("FPK cmd/uninstall_callback must finish without post-cleanup mutation")

    lifecycle = "\n".join(scripts.values()).lower()
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
        if forbidden in lifecycle:
            raise ValueError(f"FPK lifecycle contains prohibited host mutation: {forbidden}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fpk", type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--version", required=True, type=release_version)
    args = parser.parse_args()

    with tarfile.open(args.fpk, "r:gz") as archive:
        package_members = validated_archive_members(archive, "FPK")
        names = set(package_members)
        missing = sorted(REQUIRED_FILES - names)
        if missing:
            raise ValueError(f"FPK is missing required files: {', '.join(missing)}")
        unexpected_package_files = sorted(
            name
            for name, member in package_members.items()
            if member.isfile() and name not in REQUIRED_FILES
        )
        if unexpected_package_files:
            raise ValueError(
                "FPK contains unexpected package payload: "
                + ", ".join(unexpected_package_files)
            )

        manifest = configparser.ConfigParser()
        manifest_file = archive.extractfile("manifest")
        app_file = archive.extractfile("app.tgz")
        if manifest_file is None or app_file is None:
            raise ValueError("FPK archive has unreadable required content")
        manifest.read_string("[package]\n" + manifest_file.read().decode("utf-8"))
        package = manifest["package"]
        package_contents: dict[str, str] = {}
        command_scripts = {
            name for name in names if name.startswith("cmd/") and not name.endswith("/")
        }
        for required in (REQUIRED_PACKAGE_TEXT_FILES - {"manifest"}) | command_scripts:
            member = archive.extractfile(required)
            if member is None:
                raise ValueError(f"FPK package file is unreadable: {required}")
            package_contents[required] = member.read().decode("utf-8")

        with tarfile.open(fileobj=io.BytesIO(app_file.read()), mode="r:gz") as app_archive:
            app_members = validated_archive_members(app_archive, "FPK app payload")
            app_names = set(app_members)
            missing_app_files = sorted(REQUIRED_APP_FILES - app_names)
            if missing_app_files:
                raise ValueError(
                    "FPK app payload is missing required file: "
                    + ", ".join(missing_app_files)
                )
            unexpected_app_files = sorted(
                name
                for name, member in app_members.items()
                if member.isfile() and name not in REQUIRED_APP_FILES
            )
            if unexpected_app_files:
                raise ValueError(
                    "FPK app payload contains unexpected file: "
                    + ", ".join(unexpected_app_files)
                )
            app_contents: dict[str, bytes] = {}
            for required in REQUIRED_APP_FILES:
                member = app_archive.extractfile(required)
                if member is None:
                    raise ValueError(f"FPK app payload file is unreadable: {required}")
                app_contents[required] = member.read()

        for generated_config in ("config/privilege", "config/resource"):
            if app_contents[generated_config] != package_contents[generated_config].encode("utf-8"):
                raise ValueError(
                    f"FPK app payload generated config copy does not match package: {generated_config}"
                )

        default_service = parse_compose_service(
            app_contents["docker/docker-compose.yaml"].decode("utf-8"),
            "default Compose",
        )
        no_device_service = parse_compose_service(
            app_contents["docker/docker-compose.no-device.yaml"].decode("utf-8"),
            "no-device Compose",
        )
        device_service = parse_compose_service(
            app_contents["docker/docker-compose.device.yaml"].decode("utf-8"),
            "device overlay",
        )
        for compose_name, service in (
            ("default Compose", default_service),
            ("no-device Compose", no_device_service),
            ("device overlay", device_service),
        ):
            validate_compose_image(service, compose_name, args.image)
            validate_compose_safety(service, compose_name, args.image, args.version)
        validate_no_device_compose(default_service, "default Compose")
        validate_no_device_compose(no_device_service, "no-device Compose")
        validate_device_compose(device_service)
        validate_readonly_poc(app_contents["diagnostics/fnos-readonly-poc.sh"].decode("utf-8"))
        validate_platform_poc(app_contents["diagnostics/fnos-platform-poc.sh"].decode("utf-8"))
        validate_gateway_launcher(json.loads(app_contents["ui/config"].decode("utf-8")))
        validate_manifest(package, args.version)
        validate_resource(package_contents["config/resource"])
        validate_privilege(package_contents["config/privilege"])
        validate_device_wizard(package_contents["wizard/install"], "wizard/install")
        validate_device_wizard(package_contents["wizard/config"], "wizard/config")
        validate_uninstall_wizard(package_contents["wizard/uninstall"])
        validate_lifecycle_scripts(package_contents)

    print(f"verified {args.fpk}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"FPK verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
