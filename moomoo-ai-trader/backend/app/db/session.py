"""数据库连接。

- 每个命名空间一个 SQLite 文件（data/sim.db, data/live.db），WAL 模式。
- 写事务使用 BEGIN IMMEDIATE：并发写入方在事务开始时就串行化，
  防止两个并发建议分别读到同一笔可用资金后一起超额下单。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings
from ..core.enums import Namespace

_lock = threading.Lock()
_engines: dict[tuple[str, str], Engine] = {}
_makers: dict[tuple[str, str], sessionmaker] = {}


def _make_engine(path: Path, immediate: bool) -> Engine:
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):  # pragma: no cover - driver hook
        dbapi_conn.isolation_level = None  # 交给我们手动 BEGIN
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=FULL")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):  # pragma: no cover - driver hook
        conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine


def get_engine(namespace: Namespace | str, *, immediate: bool = True) -> Engine:
    ns = Namespace(namespace).value
    key = (ns, "w" if immediate else "r")
    with _lock:
        if key not in _engines:
            path = get_settings().db_path(ns)
            path.parent.mkdir(parents=True, exist_ok=True)
            _engines[key] = _make_engine(path, immediate)
            _makers[key] = sessionmaker(bind=_engines[key], expire_on_commit=False, future=True)
        return _engines[key]


@contextmanager
def write_session(namespace: Namespace | str) -> Iterator[Session]:
    """写事务（BEGIN IMMEDIATE），退出时提交，异常时回滚。"""
    get_engine(namespace, immediate=True)
    maker = _makers[(Namespace(namespace).value, "w")]
    s = maker()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


@contextmanager
def read_session(namespace: Namespace | str) -> Iterator[Session]:
    get_engine(namespace, immediate=False)
    maker = _makers[(Namespace(namespace).value, "r")]
    s = maker()
    try:
        yield s
    finally:
        s.close()  # 关闭即回滚连接上的只读事务；已加载的对象保持可读（不会被标记过期）


def dispose_all() -> None:
    with _lock:
        for e in _engines.values():
            e.dispose()
        _engines.clear()
        _makers.clear()


def migrate(namespace: Namespace | str) -> None:
    """运行 Alembic 迁移到最新版本，并写入/校验命名空间标记。"""
    from alembic import command
    from alembic.config import Config

    ns = Namespace(namespace).value
    backend_dir = Path(__file__).resolve().parents[2]
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{get_settings().db_path(ns)}")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")

    from .models import DbMeta

    with write_session(ns) as s:
        meta = s.get(DbMeta, "namespace")
        if meta is None:
            s.add(DbMeta(key="namespace", value=ns))
        elif meta.value != ns:
            raise RuntimeError(f"数据库文件命名空间不符：期望 {ns}，实际 {meta.value}。拒绝启动。")


def check_namespace(s: Session, ns: str) -> None:
    from .models import DbMeta

    v = s.execute(select(DbMeta.value).where(DbMeta.key == "namespace")).scalar_one_or_none()
    if v != ns:
        raise RuntimeError(f"数据库命名空间不符：期望 {ns}，实际 {v}")
