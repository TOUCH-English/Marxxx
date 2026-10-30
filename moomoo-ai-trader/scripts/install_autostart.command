#!/bin/bash
# macOS：安装「登录 Mac 后自动启动、异常退出自动重启」。请在美股收盘后、先停止系统再运行。
cd "$(dirname "$0")/.." || exit 1
PY=python3; command -v python3 >/dev/null || PY=python
"$PY" scripts/launcher.py install-autostart
