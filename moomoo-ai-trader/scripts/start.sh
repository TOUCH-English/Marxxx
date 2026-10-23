#!/usr/bin/env bash
# macOS / Linux 一键启动
cd "$(dirname "$0")/.." || exit 1
PY=python3; command -v python3 >/dev/null || PY=python
exec "$PY" scripts/launcher.py start
