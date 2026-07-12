#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python3 -m py_compile lib/*.py scripts/*.py tests/*.py
python3 tests/check_version.py
python3 tests/validate_config_schema.py
python3 tests/test_pdu_encoder.py
python3 tests/test_pdu_decoder.py
python3 tests/test_web_api.py
python3 tests/test_web_auth.py
python3 tests/test_web_auth_integration.py
python3 tests/test_web_history.py
python3 tests/test_fnos_runtime_contract.py
python3 tests/test_fnos_gid_contract.py
python3 tests/test_fnos_fpk_contract.py
python3 tests/test_fnos_device_mode_contract.py
python3 tests/test_fnos_readonly_poc_contract.py
python3 tests/test_fnos_platform_poc_contract.py
python3 tests/test_windows_recovery_contract.py
python3 tests/test_nas_deployment_contract.py
bash tests/test_shell_behaviors.sh

for file in bin/cardpulse lib/*.sh scripts/*.sh; do
    bash -n "$file"
done
