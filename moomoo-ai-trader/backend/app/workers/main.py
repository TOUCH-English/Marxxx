"""后台交易工作进程入口：python -m app.workers.main --ns sim

- 每个命名空间只允许一个工作进程：文件锁 + 数据库心跳租约双重互斥。
- Web 重启不会启动第二个交易实例（交易只在本进程中进行）。
- 实盘命名空间只有在部署配置 LIVE_TRADING_ENABLED=true 时才启动。
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import time
from datetime import timedelta

from filelock import FileLock, Timeout

from ..config import get_settings
from ..db.models import WorkerHeartbeat
from ..db.session import write_session
from ..db.types import utcnow
from ..services.runs import ensure_seed
from . import downtime
from .engine import TradingEngine

log = logging.getLogger("worker")
LEASE_TTL = timedelta(seconds=30)
PAUSE_GAP = timedelta(minutes=3)  # 正常每轮约 2 秒；超过 3 分钟说明进程被挂起或卡住


def acquire_lease(ns: str, owner: str) -> bool:
    """数据库租约：已有其他所有者且心跳未过期时拒绝。"""
    now = utcnow()
    with write_session(ns) as s:
        hb = s.get(WorkerHeartbeat, ns)
        if hb is not None and hb.owner != owner and now - hb.heartbeat_at < LEASE_TTL:
            return False
        if hb is None:
            s.add(WorkerHeartbeat(name=ns, owner=owner, pid=os.getpid(), started_at=now, heartbeat_at=now))
        else:
            hb.owner, hb.pid, hb.started_at, hb.heartbeat_at = owner, os.getpid(), now, now
    return True


def last_seen(ns: str):
    """上一个工作进程最后一次心跳的时间，以及它是否正常停止。没有记录返回 (None, False)。"""
    with write_session(ns) as s:
        hb = s.get(WorkerHeartbeat, ns)
        if hb is None:
            return None, False
        if hb.owner.startswith("released:"):  # 正常停止时 release_lease 把心跳往前拨了 2 个租约周期
            return hb.heartbeat_at + LEASE_TTL * 2, True
        return hb.heartbeat_at, False


def _record_gap(ns: str, start, end, reason: str) -> None:
    try:
        downtime.record_downtime(ns, start, end, reason)
    except Exception:  # 停机记录失败不能影响交易保护
        log.exception("停机记录失败")


def release_lease(ns: str, owner: str) -> None:
    """正常退出时释放租约，使停止后立刻重启的新进程无需等待过期。"""
    with write_session(ns) as s:
        hb = s.get(WorkerHeartbeat, ns)
        if hb is not None and hb.owner == owner:
            hb.heartbeat_at = hb.heartbeat_at - LEASE_TTL * 2
            hb.owner = f"released:{owner}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ns", choices=["sim", "live"], default="sim")
    ap.add_argument("--max-ticks", type=int, default=0, help="测试用：运行指定次数后退出")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    st = get_settings()
    if args.ns == "live" and not st.live_trading_enabled:
        log.info("实盘工作进程未启动：部署配置 LIVE_TRADING_ENABLED=false（默认关闭）")
        return 0
    ensure_seed(args.ns)
    lock = FileLock(str(st.data_dir / f"{args.ns}.worker.lock"))
    try:
        lock.acquire(timeout=1)
    except Timeout:
        log.error("已有 %s 工作进程在运行（文件锁被占用），本进程退出", args.ns)
        return 2
    owner = f"{socket.gethostname()}:{os.getpid()}"
    prev_seen, prev_clean = last_seen(args.ns)
    # 已拿到文件锁 = 本机没有其他工作进程；租约可能是上一个被强制结束的进程留下的，等待其过期后接管
    deadline = time.monotonic() + LEASE_TTL.total_seconds() + 5
    while not acquire_lease(args.ns, owner):
        if time.monotonic() > deadline:
            log.error("数据库租约显示已有其他工作进程在运行，本进程退出")
            lock.release()
            return 3
        log.info("等待上一个工作进程的租约过期…")
        time.sleep(2)
    if prev_seen is not None:
        _record_gap(args.ns, prev_seen, utcnow(),
                    "上次是正常停止后重新启动" if prev_clean else "上次异常中断（电脑重启、程序崩溃或被强制结束）")
    stop = {"flag": False}

    def _sig(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    eng = TradingEngine(args.ns, worker_id=owner)
    n = 0
    log.info("%s 工作进程启动（%s）", args.ns, owner)
    last_wall = utcnow()
    try:
        while not stop["flag"]:
            t0 = time.monotonic()
            now_wall = utcnow()
            # 从上一轮开始算（包括上一轮的 tick）：电脑在 tick 中途睡眠也能发现
            if now_wall - last_wall > PAUSE_GAP:
                _record_gap(args.ns, last_wall, now_wall, "工作进程被暂停（多半是电脑睡眠）")
            last_wall = now_wall
            try:
                eng.tick()
            except Exception:
                log.exception("tick error")
            eng.heartbeat(owner, os.getpid())
            n += 1
            if args.max_ticks and n >= args.max_ticks:
                break
            time.sleep(max(0.05, st.worker_tick_seconds - (time.monotonic() - t0)))
    finally:
        try:
            release_lease(args.ns, owner)
        finally:
            lock.release()
        log.info("%s 工作进程退出", args.ns)
    return 0


if __name__ == "__main__":
    sys.exit(main())
