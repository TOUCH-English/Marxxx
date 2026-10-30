"""开机自启守护进程（scripts/launcher.py supervise）：崩溃自动重启、停止时结束子进程、不重复启动、尊重用户停止。"""
import importlib.util
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _launcher():
    spec = importlib.util.spec_from_file_location("launcher", ROOT / "scripts" / "launcher.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(mod, tmp_path, cmds, seconds):
    stop = threading.Event()
    threading.Timer(seconds, stop.set).start()
    code = mod.supervise(cmds, run_dir=tmp_path, cwd=tmp_path, check_s=0.1, max_backoff_s=0.4,
                         stop_event=stop, keep_awake=False)
    return code


def test_crashed_child_is_restarted_and_children_stop_with_supervisor(tmp_path):
    mod = _launcher()
    starts = tmp_path / "starts.txt"
    crash = [sys.executable, "-c", f"open(r'{starts}','a').write('x'); import time; time.sleep(0.2); raise SystemExit(1)"]
    steady = [sys.executable, "-c", "import time; time.sleep(60)"]
    t0 = time.monotonic()
    code = _run(mod, tmp_path, {"worker_sim": crash, "web": steady}, 8.0)  # 第一次重启在约 5 秒后
    assert code == 0
    assert starts.read_text().count("x") >= 2, "崩溃的子进程应被重新启动"
    assert time.monotonic() - t0 < 40
    # 停止后子进程全部结束、pid 文件清理
    assert not list(tmp_path.glob("*.pid"))


def test_refuses_when_processes_already_running_or_user_stopped(tmp_path):
    mod = _launcher()
    import subprocess

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "fake_web_marker"])
    try:
        (tmp_path / "web.pid").write_text(str(other.pid))
        # pid 活着但不是本系统的进程（电脑重启后 pid 被重用）：照常启动
        cmds = {"web": [sys.executable, "-c", "open('started','w')", "app.api.app"]}
        stop = threading.Event()
        threading.Timer(1.0, stop.set).start()
        assert mod.supervise(cmds, run_dir=tmp_path, cwd=tmp_path, check_s=0.1, stop_event=stop, keep_awake=False) == 0
        assert (tmp_path / "started").exists()
        (tmp_path / "started").unlink()
        # pid 活着且是本系统的进程（手动启动的）：不重复启动
        (tmp_path / "web.pid").write_text(str(other.pid))
        guard = threading.Event()
        threading.Timer(3.0, guard.set).start()  # 万一判断错误而启动了，也不会让测试卡住
        assert mod.supervise({"web": [sys.executable, "-c", "open('started','w')", "fake_web_marker"]},
                             run_dir=tmp_path, cwd=tmp_path, stop_event=guard, keep_awake=False) == 0
        assert not (tmp_path / "started").exists()
    finally:
        other.kill()
    (tmp_path / "web.pid").unlink(missing_ok=True)
    (tmp_path / "STOPPED_BY_USER").write_text("x")
    assert mod.supervise(cmds, run_dir=tmp_path, cwd=tmp_path, keep_awake=False) == 0
    assert not (tmp_path / "started").exists()


def test_launch_agent_restarts_only_on_crash_and_starts_at_login():
    import plistlib

    mod = _launcher()
    p = mod.agent_plist()
    plistlib.loads(plistlib.dumps(p))  # 能生成合法的 plist
    assert p["RunAtLoad"] is True
    assert p["KeepAlive"] == {"SuccessfulExit": False}  # 正常退出（stop / 用户停止）不会被拉起
    assert p["ProgramArguments"][-1] == "supervise" and p["ProgramArguments"][1].endswith("scripts/launcher.py")
    assert p["Label"] == mod.AGENT_LABEL and mod.AGENT_PLIST.name == f"{mod.AGENT_LABEL}.plist"
