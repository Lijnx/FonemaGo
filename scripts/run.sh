#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
umask 077

if [[ ! -x .venv/bin/python ]]; then
    echo 'Окружение не установлено. Выполните uv sync --frozen (см. docs/run-linux.md).' >&2
    exit 1
fi

# No dependency resolution, network package downloads or shell activation on restart.
exec .venv/bin/python -m fonemago.cli run
