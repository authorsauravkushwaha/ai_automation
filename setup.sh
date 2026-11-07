#!/usr/bin/env bash
# Portable core setup for development/non-Windows diagnostics.
# Windows 11 users should run setup.cmd -Run instead.
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required"'
if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -e .
[[ -f config.toml ]] || cp config.example.toml config.toml
.venv/bin/python scripts/run_tests.py
.venv/bin/python -m hinata --no-llm ask system info
cat <<'EOF'

HINATA core setup passed.
Run: .venv/bin/python -m hinata --no-llm
Ollama and voice/desktop extras are separate on non-Windows development systems.
EOF
