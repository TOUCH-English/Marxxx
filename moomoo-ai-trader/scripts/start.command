#!/bin/bash
# macOS：在「访达」中双击即可启动（首次可能需要右键 → 打开）
cd "$(dirname "$0")/.." || exit 1
PY=python3; command -v python3 >/dev/null || PY=python
"$PY" scripts/launcher.py start
echo ""
echo "可以关闭这个窗口；系统会在后台继续运行。停止请双击 scripts/stop.command"
