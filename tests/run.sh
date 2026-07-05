#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python3 -m py_compile lib/pdu_encoder.py tests/*.py
python3 tests/check_version.py
python3 tests/validate_config_schema.py
python3 tests/test_pdu_encoder.py
bash tests/test_shell_behaviors.sh

for file in bin/cardpulse lib/*.sh scripts/install.sh; do
    bash -n "$file"
done
