#!/usr/bin/env bash
cd "$(dirname "$0")/.." || exit 1
PY=python3; command -v python3 >/dev/null || PY=python
exec "$PY" scripts/launcher.py stop
