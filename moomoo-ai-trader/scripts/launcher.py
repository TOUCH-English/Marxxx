"""跨平台启动/停止脚本（Windows / macOS / Linux 通用）。

用法：
  python scripts/launcher.py start   # 检查环境 → 安装依赖 → 初始化数据库 → 启动网页与后台工作进程 → 打开浏览器
  python scripts/launcher.py stop    # 停止
  python scripts/launcher.py status
一般直接双击/运行 scripts/start.bat（Windows）或 scripts/start.sh（macOS/Linux）。
"""
from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "data" / "run"
IS_WIN = platform.system() == "Windows"
VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")


def say(msg: str) -> None:
    print(f"[启动器] {msg}", flush=True)


def check_env() -> bool:
    ok = True
    say(f"操作系统：{platform.system()} {platform.release()}（{platform.machine()}）")
    if sys.version_info < (3, 11):
        say(f"需要 Python 3.11 或以上，当前 {platform.python_version()}。请从 https://www.python.org/downloads/ 安装。")
        ok = False
    else:
        say(f"Python {platform.python_version()} ✓")
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        if shutil.which("npm"):
            say("前端未构建，将使用 npm 构建")
        else:
            say("前端未构建且未找到 Node.js（npm）。请安装 Node.js 20+ 或使用随项目提供的已构建前端。")
            ok = False
    free = shutil.disk_usage(ROOT).free / 1e9
    say(f"磁盘可用 {free:.1f} GB" + (" ✓" if free > 1 else "（偏少）"))
    return ok


def ensure_venv() -> None:
    if not VENV_PY.exists():
        say("创建虚拟环境 .venv …")
        subprocess.check_call([sys.executable, "-m", "venv", str(ROOT / ".venv")])
    stamp = ROOT / ".venv" / ".deps_installed"
    lock = ROOT / "requirements.lock.txt"
    if not stamp.exists() or stamp.read_text() != lock.read_text():
        say("安装依赖（首次约需几分钟）…")
        subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "-q", "-r", str(lock)])
        stamp.write_text(lock.read_text())
    env = ROOT / ".env"
    if not env.exists():
        shutil.copy(ROOT / ".env.example", env)
        say("已生成 .env（默认：实盘关闭、无 AI 密钥时使用固定测试响应）")


def build_frontend() -> None:
    if (ROOT / "frontend" / "dist" / "index.html").exists():
        return
    say("构建前端 …")
    npm = shutil.which("npm")
    subprocess.check_call([npm, "ci"], cwd=ROOT / "frontend", shell=IS_WIN)
    subprocess.check_call([npm, "run", "build"], cwd=ROOT / "frontend", shell=IS_WIN)


def _spawn(name: str, args: list[str]) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    log = open(ROOT / "data" / f"{name}.log", "a", encoding="utf-8")
    kw = {"cwd": str(ROOT / "backend"), "stdout": log, "stderr": subprocess.STDOUT}
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(args, **kw)
    (RUN / f"{name}.pid").write_text(str(p.pid))
    say(f"已启动 {name}（PID {p.pid}，日志 data/{name}.log）")


def _alive(pid: int) -> bool:
    try:
        if IS_WIN:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status() -> dict[str, bool]:
    out = {}
    for f in sorted(RUN.glob("*.pid")) if RUN.exists() else []:
        out[f.stem] = _alive(int(f.read_text() or 0))
    return out


def _read_env(p: Path) -> dict[str, str]:
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def start() -> int:
    if not check_env():
        return 1
    ensure_venv()
    build_frontend()
    running = {k for k, v in status().items() if v}
    subprocess.check_call([str(VENV_PY), "-c", "from app.services.runs import ensure_seed; ensure_seed('sim'); ensure_seed('live')"],
                          cwd=str(ROOT / "backend"))
    cfg = _read_env(ROOT / ".env")
    port = cfg.get("APP_PORT") or "8710"
    if "web" not in running:
        _spawn("web", [str(VENV_PY), "-m", "uvicorn", "app.api.app:app", "--host", "127.0.0.1", "--port", port])
    if "worker_sim" not in running:
        _spawn("worker_sim", [str(VENV_PY), "-m", "app.workers.main", "--ns", "sim"])
    if "worker_live" not in running and str(cfg.get("LIVE_TRADING_ENABLED", "false")).lower() == "true":
        _spawn("worker_live", [str(VENV_PY), "-m", "app.workers.main", "--ns", "live"])
    url = f"http://127.0.0.1:{port}/"
    time.sleep(4)
    for name, alive in status().items():
        if not alive:
            say(f"⚠ {name} 没有成功运行。日志最后几行：")
            logf = ROOT / "data" / f"{name}.log"
            if logf.exists():
                for line in logf.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]:
                    print("    " + line)
            say("请把上面的文字发给技术支持；也可以先运行停止脚本，等 1 分钟后再启动。")
    say(f"打开 {url}（首次需设置本机密码）")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return 0


def stop() -> int:
    for name, alive in status().items():
        pid = int((RUN / f"{name}.pid").read_text())
        if alive:
            try:
                if IS_WIN:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
                else:
                    os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass
            say(f"已停止 {name}（PID {pid}）")
        (RUN / f"{name}.pid").unlink(missing_ok=True)
    say("已全部停止。注意：停止后系统不会再执行任何保护性止损，请自行留意持仓。")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if cmd == "start":
        sys.exit(start())
    if cmd == "stop":
        sys.exit(stop())
    if cmd == "status":
        print(status())
        sys.exit(0)
    print(__doc__)
