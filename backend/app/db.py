from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app import config


def connect() -> sqlite3.Connection:
    # isolation_level=None：事务边界全部显式书写，避免隐式 BEGIN 在并发写时
    # 由 DEFERRED 升级锁失败（database is locked）。
    conn = sqlite3.connect(config.DB_PATH, timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def _unit(begin: str):
    conn = connect()
    try:
        conn.execute(begin)
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def db():
    """读或简单写：DEFERRED 事务，保持原有「整段提交」语义。"""
    with _unit("BEGIN") as conn:
        yield conn


@contextmanager
def tx():
    """写口：BEGIN IMMEDIATE 串行化同库写者，保证「第二张与运行席推进同成同败」。"""
    with _unit("BEGIN IMMEDIATE") as conn:
        yield conn


# 老库补列（全新库由 CREATE TABLE 直接带出）
_ORDER_EXTRA_COLUMNS = {
    "merged_into_id": "INTEGER",
    "coboot": "INTEGER NOT NULL DEFAULT 0",
    "extend_sec": "INTEGER NOT NULL DEFAULT 0",
    "seat_no": "INTEGER",
    "seat_stamp": "TEXT",
    "closed_at": "TEXT",
    "merge_count": "INTEGER NOT NULL DEFAULT 0",
}


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_schema(conn: sqlite3.Connection) -> None:
    # 逐条执行而非 executescript：executescript 会先隐式 COMMIT，
    # 会把调用方的写事务截断，建表与播种就不再同成同败。
    for stmt in _SCHEMA_STATEMENTS:
        conn.execute(stmt)
    _add_missing_columns(conn, "start_orders", _ORDER_EXTRA_COLUMNS)
    _add_missing_columns(conn, "run_seats", _SEAT_EXTRA_COLUMNS)


_SEAT_EXTRA_COLUMNS = {
    "prev_run_state": "TEXT NOT NULL DEFAULT '已停止'",
    "charged_cpu": "REAL NOT NULL DEFAULT 0",
    "charged_mem_mb": "INTEGER NOT NULL DEFAULT 0",
    "closed_at": "TEXT",
}

_SCHEMA_SCRIPT = """
        CREATE TABLE IF NOT EXISTS images (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          tag TEXT NOT NULL,
          pull_state TEXT NOT NULL,
          layers_done INTEGER NOT NULL DEFAULT 0,
          layers_total INTEGER NOT NULL DEFAULT 4,
          ready_at TEXT
        );
        CREATE TABLE IF NOT EXISTS pods (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          image_id INTEGER,
          run_state TEXT NOT NULL,
          sidecar TEXT,
          cpu_quota REAL NOT NULL DEFAULT 1.0,
          mem_quota_mb INTEGER NOT NULL DEFAULT 512,
          FOREIGN KEY(image_id) REFERENCES images(id)
        );
        CREATE TABLE IF NOT EXISTS start_orders (
          id INTEGER PRIMARY KEY,
          pod_id INTEGER NOT NULL,
          action TEXT NOT NULL,
          target_image_id INTEGER,
          status TEXT NOT NULL,
          note TEXT,
          created_at TEXT NOT NULL,
          merged_into_id INTEGER,
          coboot INTEGER NOT NULL DEFAULT 0,
          extend_sec INTEGER NOT NULL DEFAULT 0,
          seat_no INTEGER,
          seat_stamp TEXT,
          closed_at TEXT,
          merge_count INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY(pod_id) REFERENCES pods(id)
        );
        CREATE TABLE IF NOT EXISTS run_seats (
          id INTEGER PRIMARY KEY,
          pod_id INTEGER NOT NULL,
          order_id INTEGER NOT NULL,
          seat_no INTEGER NOT NULL,
          stamp TEXT NOT NULL,
          hold_sec INTEGER NOT NULL,
          extended_sec INTEGER NOT NULL DEFAULT 0,
          opened_at TEXT NOT NULL,
          expire_at TEXT NOT NULL,
          state TEXT NOT NULL,
          prev_run_state TEXT NOT NULL DEFAULT '已停止',
          charged_cpu REAL NOT NULL DEFAULT 0,
          charged_mem_mb INTEGER NOT NULL DEFAULT 0,
          closed_at TEXT,
          FOREIGN KEY(pod_id) REFERENCES pods(id),
          FOREIGN KEY(order_id) REFERENCES start_orders(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_run_seats_open
          ON run_seats(pod_id) WHERE state = '占用中';
        CREATE INDEX IF NOT EXISTS ix_start_orders_pod_status
          ON start_orders(pod_id, status);
        CREATE TABLE IF NOT EXISTS probes (
          id INTEGER PRIMARY KEY,
          pod_id INTEGER NOT NULL UNIQUE,
          color TEXT NOT NULL,
          fail_streak INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT,
          FOREIGN KEY(pod_id) REFERENCES pods(id)
        );
        CREATE TABLE IF NOT EXISTS quotas (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          cpu_used REAL NOT NULL,
          cpu_limit REAL NOT NULL,
          mem_used_mb INTEGER NOT NULL,
          mem_limit_mb INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS restart_policy (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          mode TEXT NOT NULL,
          backoff_sec INTEGER NOT NULL DEFAULT 5
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL,
          ref TEXT,
          detail TEXT NOT NULL,
          at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
"""

_SCHEMA_STATEMENTS = [part.strip() for part in _SCHEMA_SCRIPT.split(";") if part.strip()]
