#!/bin/bash
# macOS：卸载自动启动（会同时停止系统；之后可双击 start.command 按原方式启动）
cd "$(dirname "$0")/.." || exit 1
PY=python3; command -v python3 >/dev/null || PY=python
"$PY" scripts/launcher.py uninstall-autostart
