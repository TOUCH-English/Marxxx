"""跨平台启动/停止脚本（Windows / macOS / Linux 通用）。

用法：
  python scripts/launcher.py start   # 检查环境 → 安装依赖 → 初始化数据库 → 启动网页与后台工作进程 → 打开浏览器
  python scripts/launcher.py stop    # 停止
  python scripts/launcher.py status
  python scripts/launcher.py supervise   # 前台守护：启动网页与工作进程，异常退出时自动重启（供 macOS LaunchAgent 使用）
  python scripts/launcher.py install-autostart     # macOS：登录后自动启动、异常退出自动重启（只影响当前用户）
  python scripts/launcher.py uninstall-autostart   # macOS：卸载自动启动（会同时停止系统）
一般直接双击/运行 scripts/start.bat（Windows）或 scripts/start.sh（macOS/Linux）。

macOS 已安装开机自启（scripts/macos/install_autostart.sh）时，start / stop 会改为通过 launchctl 启停守护进程：
- stop 会卸载守护进程并写入「用户已停止」标记，之后即使重新登录 Mac 也不会自动启动，直到再次 start；
- 守护进程只在异常退出时被 launchd 重新拉起（KeepAlive SuccessfulExit=false）。
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
AGENT_LABEL = "com.moomoo-ai-trader.supervisor"
AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
STOPPED_FLAG = RUN / "STOPPED_BY_USER"
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


def _is_ours(pid: int, argv: list[str]) -> bool:
    """pid 文件里的进程还活着，并且确实是本系统的进程（电脑重启后 pid 可能被别的程序重用）。"""
    if not _alive(pid):
        return False
    if IS_WIN:
        return True
    r = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    if r.returncode != 0:
        return False
    key = next((a for a in argv if a.startswith("app.") or a.endswith(".py")), argv[-1])
    return key in r.stdout


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


def _agent_installed() -> bool:
    return platform.system() == "Darwin" and AGENT_PLIST.exists()


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _agent_loaded() -> bool:
    return _launchctl("print", f"gui/{os.getuid()}/{AGENT_LABEL}").returncode == 0


def start() -> int:
    if _agent_installed():
        STOPPED_FLAG.unlink(missing_ok=True)
        if not _agent_loaded():
            r = _launchctl("bootstrap", f"gui/{os.getuid()}", str(AGENT_PLIST))
            if r.returncode != 0:
                say(f"启动守护进程失败：{r.stderr.strip() or r.stdout.strip()}")
                return 1
        else:
            _launchctl("kickstart", f"gui/{os.getuid()}/{AGENT_LABEL}")
        say("已通过开机自启守护进程启动（异常退出会自动重启）。查看状态：python scripts/launcher.py status")
        port = _read_env(ROOT / ".env").get("APP_PORT") or "8710"
        try:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        except Exception:
            pass
        return 0
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
    if _agent_installed():
        RUN.mkdir(parents=True, exist_ok=True)
        STOPPED_FLAG.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))  # 重新登录后也不自动启动，直到再次 start
        if _agent_loaded():
            _launchctl("bootout", f"gui/{os.getuid()}/{AGENT_LABEL}")  # 卸载后 KeepAlive 不会再拉起
            for _ in range(40):
                if not any(status().values()):
                    break
                time.sleep(1)
    for name, alive in status().items():
        pid = int((RUN / f"{name}.pid").read_text())
        if alive:
            try:
                if IS_WIN:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
                else:
                    try:
                        os.killpg(pid, signal.SIGTERM)
                    except OSError:  # 由守护进程启动的子进程不是进程组组长
                        os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            say(f"已停止 {name}（PID {pid}）")
        (RUN / f"{name}.pid").unlink(missing_ok=True)
    say("已全部停止。注意：停止后系统不会再执行任何保护性止损，请自行留意持仓。")
    return 0


# ------------------------------------------------------------------ macOS 开机自启（LaunchAgent）
def agent_plist() -> dict:
    """LaunchAgent 配置：登录后启动；只有异常退出（非 0 / 崩溃）才重启，正常停止不会被拉起。"""
    return {
        "Label": AGENT_LABEL,
        "ProgramArguments": [str(VENV_PY), str(ROOT / "scripts" / "launcher.py"), "supervise"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Interactive",  # 避免后台节流（App Nap）拖慢行情与止损
        "StandardOutPath": str(ROOT / "data" / "supervisor.log"),
        "StandardErrorPath": str(ROOT / "data" / "supervisor.log"),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"},
    }


def install_autostart() -> int:
    import plistlib

    if platform.system() != "Darwin":
        say("自动启动只支持 macOS")
        return 1
    if not VENV_PY.exists():
        say(f"找不到 {VENV_PY}：请先用 scripts/start.command 完成首次安装")
        return 1
    running = [k for k, v in status().items() if v]
    if running and not _agent_loaded():
        say(f"检测到系统正在运行（手动启动：{', '.join(running)}）。请在美股收盘后先运行 stop，再安装。")
        return 2
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "run").mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.write_bytes(plistlib.dumps(agent_plist()))
    STOPPED_FLAG.unlink(missing_ok=True)
    if _agent_loaded():
        _launchctl("bootout", f"gui/{os.getuid()}/{AGENT_LABEL}")
        time.sleep(3)
    r = _launchctl("bootstrap", f"gui/{os.getuid()}", str(AGENT_PLIST))
    if r.returncode != 0:
        say(f"launchctl bootstrap 失败：{r.stderr.strip() or r.stdout.strip()}")
        return 1
    say(f"已安装：{AGENT_PLIST}")
    say("登录 Mac 后会自动启动；网页或工作进程异常退出会自动重启；运行 stop 后不会被自动拉起。")
    return 0


def uninstall_autostart() -> int:
    if _agent_loaded():
        _launchctl("bootout", f"gui/{os.getuid()}/{AGENT_LABEL}")
    AGENT_PLIST.unlink(missing_ok=True)
    say("已卸载自动启动（系统也已停止）。需要时双击 scripts/start.command 按原方式启动。")
    return 0


# ------------------------------------------------------------------ 前台守护（macOS LaunchAgent）
def _commands(cfg: dict[str, str]) -> dict[str, list[str]]:
    port = cfg.get("APP_PORT") or "8710"
    out = {"web": [str(VENV_PY), "-m", "uvicorn", "app.api.app:app", "--host", "127.0.0.1", "--port", port],
           "worker_sim": [str(VENV_PY), "-m", "app.workers.main", "--ns", "sim"]}
    if str(cfg.get("LIVE_TRADING_ENABLED", "false")).lower() == "true":
        out["worker_live"] = [str(VENV_PY), "-m", "app.workers.main", "--ns", "live"]
    return out


def supervise(commands: dict[str, list[str]] | None = None, *, run_dir: Path | None = None, cwd: Path | None = None,
              check_s: float = 5.0, max_backoff_s: float = 60.0, stop_event=None, keep_awake: bool = True) -> int:
    """前台运行，启动并看守网页与工作进程。

    - 已有手动启动的进程在运行、或用户已手动停止：直接正常退出（退出码 0，launchd 不会重启）；
    - 子进程退出就按退避时间重启（5 秒起，最多 60 秒）；同一命名空间重复的工作进程会被文件锁拒绝；
    - 收到 SIGTERM（launchctl bootout / 关机）时先请子进程正常退出，最多等 30 秒，然后退出码 0。
    """
    import threading

    run = run_dir or RUN
    run.mkdir(parents=True, exist_ok=True)
    if (run / STOPPED_FLAG.name).exists():
        say("用户已手动停止系统（存在 STOPPED_BY_USER 标记），守护进程不启动。需要时运行 start。")
        return 0
    cfg = _read_env(ROOT / ".env") if (ROOT / ".env").exists() else {}
    cmds = commands if commands is not None else _commands(cfg)
    for name in cmds:
        f = run / f"{name}.pid"
        if f.exists() and f.read_text().strip().isdigit() and _is_ours(int(f.read_text()), cmds[name]):
            say(f"{name} 已在运行（PID {f.read_text().strip()}，可能是手动启动的）：守护进程不重复启动。请先运行 stop。")
            return 0
    if commands is None:
        subprocess.check_call([str(VENV_PY), "-c", "from app.services.runs import ensure_seed; ensure_seed('sim'); ensure_seed('live')"],
                              cwd=str(ROOT / "backend"))
    stop = stop_event or threading.Event()
    if stop_event is None:
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    (run / "supervisor.pid").write_text(str(os.getpid()))
    awake = None
    if keep_awake and platform.system() == "Darwin" and shutil.which("caffeinate"):
        awake = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])  # 守护进程在运行时阻止闲置睡眠
    procs: dict[str, subprocess.Popen] = {}
    started: dict[str, float] = {}
    restart_at: dict[str, float | None] = {}
    backoff: dict[str, float] = {name: 5.0 for name in cmds}

    def launch(name: str) -> None:
        log = open((run.parent if run_dir is None else run) / f"{name}.log", "a", encoding="utf-8")
        # 不新建进程组：守护进程被 launchd 结束时，子进程一起结束，不会留下没人看守的进程
        p = subprocess.Popen(cmds[name], cwd=str(cwd or (ROOT / "backend")), stdout=log, stderr=subprocess.STDOUT)
        procs[name], started[name], restart_at[name] = p, time.monotonic(), None
        (run / f"{name}.pid").write_text(str(p.pid))
        say(f"已启动 {name}（PID {p.pid}）")

    for name in cmds:
        launch(name)
    try:
        while not stop.wait(check_s):
            now = time.monotonic()
            for name, p in list(procs.items()):
                code = p.poll()
                if code is None:
                    if now - started[name] > 300:
                        backoff[name] = 5.0  # 稳定运行 5 分钟后重置退避
                elif restart_at[name] is None:
                    restart_at[name] = now + backoff[name]
                    say(f"⚠ {name} 已退出（退出码 {code}），{backoff[name]:.0f} 秒后重启")
                elif now >= restart_at[name]:
                    backoff[name] = min(max_backoff_s, backoff[name] * 2)
                    launch(name)
    finally:
        for name, p in procs.items():
            if p.poll() is None:
                p.terminate()
        deadline = time.monotonic() + 30
        for name, p in procs.items():
            try:
                p.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()
            (run / f"{name}.pid").unlink(missing_ok=True)
            say(f"已停止 {name}")
        (run / "supervisor.pid").unlink(missing_ok=True)
        if awake is not None and awake.poll() is None:
            awake.terminate()
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if cmd == "supervise":
        sys.exit(supervise())
    if cmd == "install-autostart":
        sys.exit(install_autostart())
    if cmd == "uninstall-autostart":
        sys.exit(uninstall_autostart())
    if cmd == "start":
        sys.exit(start())
    if cmd == "stop":
        sys.exit(stop())
    if cmd == "status":
        print(status())
        sys.exit(0)
    print(__doc__)
