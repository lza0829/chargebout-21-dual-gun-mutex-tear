from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH, HOLDING_SESSIONS

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  address TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rate_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  peak_price REAL NOT NULL,
  flat_price REAL NOT NULL,
  valley_price REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS piles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  rated_kw REAL NOT NULL DEFAULT 7,
  status TEXT NOT NULL DEFAULT '空闲',
  rate_plan_id INTEGER NOT NULL,
  UNIQUE(site_id, name),
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS guns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pile_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '可用',
  UNIQUE(pile_id, name),
  FOREIGN KEY(pile_id) REFERENCES piles(id)
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  site_id INTEGER NOT NULL,
  pile_id INTEGER NOT NULL,
  gun_id INTEGER NOT NULL,
  rate_plan_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  kwh REAL NOT NULL DEFAULT 0,
  amount REAL NOT NULL DEFAULT 0,
  price_locked REAL,
  started_at TEXT NOT NULL,
  closed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(pile_id) REFERENCES piles(id),
  FOREIGN KEY(gun_id) REFERENCES guns(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS pulses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  delta_kwh REAL NOT NULL,
  power_kw REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS session_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS open_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pile_id INTEGER,
  gun_id INTEGER,
  session_id INTEGER,
  slip_no TEXT,
  result TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  rule TEXT NOT NULL DEFAULT '',
  candidates TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
"""

# 同桩互斥单充的数据库兜底：并发写入时即使服务层判断被绕过，也至多落一笔持有会话。
HOLDING_JSON = "(" + ",".join("'" + s + "'" for s in HOLDING_SESSIONS) + ")"
INDEX_SQL = (
    f"""CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_pile_holding
        ON sessions(pile_id) WHERE status IN {HOLDING_JSON}""",
    f"""CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_gun_holding
        ON sessions(gun_id) WHERE status IN {HOLDING_JSON}""",
    "CREATE INDEX IF NOT EXISTS ix_open_audit_pile ON open_audit(pile_id, id)",
)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextmanager
def db(immediate: bool = False):
    """One explicit transaction per unit of work.

    Write paths pass ``immediate=True`` so SQLite takes the write lock up front;
    concurrent 开充 attempts serialize and the loser observes the committed
    session instead of both passing the occupancy check.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def _reconcile_legacy_duplicates(conn: sqlite3.Connection) -> None:
    """Collapse pre-existing duplicate holding sessions before adding the unique indexes.

    The app has only ever been allowed one holding session per pile under the
    chosen policy; older/hand-edited data may contain duplicates. Keep the
    earliest session per pile and 中断 the rest so the invariant can be enforced.
    """
    placeholders = ",".join("?" for _ in HOLDING_SESSIONS)
    dup_piles = conn.execute(
        f"""SELECT pile_id FROM sessions WHERE status IN ({placeholders})
            GROUP BY pile_id HAVING COUNT(*) > 1""",
        HOLDING_SESSIONS,
    ).fetchall()
    for row in dup_piles:
        keep = conn.execute(
            f"""SELECT id FROM sessions WHERE pile_id=? AND status IN ({placeholders})
                ORDER BY id LIMIT 1""",
            (row["pile_id"], *HOLDING_SESSIONS),
        ).fetchone()
        losers = conn.execute(
            f"""SELECT id FROM sessions WHERE pile_id=? AND status IN ({placeholders})
                AND id<>?""",
            (row["pile_id"], *HOLDING_SESSIONS, keep["id"]),
        ).fetchall()
        for loser in losers:
            conn.execute(
                "UPDATE sessions SET status='已中断', closed_at=datetime('now','localtime'), note=? WHERE id=?",
                ("互斥单充校正：同桩重复在充会话自动中断", loser["id"]),
            )
            conn.execute(
                "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,datetime('now','localtime'),?)",
                (loser["id"], "校正中断", "互斥单充口径：同桩至多一笔在充"),
            )


def _normalize_derived_occupancy(conn: sqlite3.Connection) -> None:
    """桩位占用态改为由持有会话推导，存储列只保留手工态。

    旧实现把「占用」写进 piles.status，半截失败会残留错占用。现在存储列只
    承载空闲/故障/维护，历史遗留的「占用」一律回正为空闲——真实占用由持有
    会话推导，因此不会丢失占用语义，也不会留下无会话的假占用。
    """
    conn.execute("UPDATE piles SET status='空闲' WHERE status='占用'")
    conn.execute("UPDATE guns SET status='可用' WHERE status='占用'")


def _purge_fake_surface(conn: sqlite3.Connection) -> None:
    """清创「假并枪」表层残留，让已撕裂的库随启动收敛回互斥单充口径。

    历史撕裂层会把粉刷过的总览计数写进 settings（surface_last/surface_ok），
    并给同桩另一枪留下 mutex_twin:* 痕迹键；这些键都不是事实来源——总览、
    桩位页、会话册一律由持有会话现场推导。旁路写进 guns.status 的「占用」
    由 _normalize_derived_occupancy 一并回正。
    """
    conn.execute(
        "DELETE FROM settings WHERE key IN ('surface_last','surface_ok') OR key LIKE 'mutex_twin:%'"
    )


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            _normalize_derived_occupancy(conn)
            _purge_fake_surface(conn)
            _reconcile_legacy_duplicates(conn)
            for stmt in INDEX_SQL:
                conn.execute(stmt)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows(rs) -> list[dict]:
    return [row_dict(r) for r in rs]
