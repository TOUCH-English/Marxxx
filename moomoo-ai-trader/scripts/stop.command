#!/bin/bash
# macOS：双击停止
cd "$(dirname "$0")/.." || exit 1
PY=python3; command -v python3 >/dev/null || PY=python
"$PY" scripts/launcher.py stop
