from __future__ import annotations
import threading

import pytest

from app.db import db, init_db
from app.services import sessions as svc


def _ids(pile_name: str) -> tuple[int, int, int]:
    with db() as conn:
        pile = conn.execute("SELECT id FROM piles WHERE name=?", (pile_name,)).fetchone()
        guns = conn.execute("SELECT id FROM guns WHERE pile_id=? ORDER BY id", (pile["id"],)).fetchall()
        return pile["id"], guns[0]["id"], guns[1]["id"]


def test_concurrent_service_calls_only_one_session(client):
    """绕过 HTTP，两个线程各自独立连接同时开充：唯一索引保证只落一笔。"""
    pile_id, left, right = _ids("河甲三号")
    outcomes: list[dict] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def attempt(gun_id: int):
        barrier.wait()
        with db(immediate=True) as conn:
            res = svc.open_session(conn, pile_id, gun_id)
        with lock:
            outcomes.append(res)

    threads = [threading.Thread(target=attempt, args=(g,)) for g in (left, right)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = [o for o in outcomes if o["成功"]]
    bad = [o for o in outcomes if not o["成功"]]
    assert len(ok) == 1
    assert len(bad) == 1
    with db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE pile_id=? AND status IN ('充电中','结算中','已超时')",
            (pile_id,),
        ).fetchone()["n"]
    assert n == 1


def test_unique_index_blocks_manual_duplicate(client):
    """直接插入第二笔持有会话会被数据库拒绝——谓词被绕过的最后防线。"""
    import sqlite3

    pile_id, left, right = _ids("河甲三号")
    with db(immediate=True) as conn:
        svc.open_session(conn, pile_id, left)
    with pytest.raises(sqlite3.IntegrityError):
        with db(immediate=True) as conn:
            conn.execute(
                """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,
                                        kwh,amount,price_locked,started_at,note)
                   VALUES('手工', 1, ?, ?, 1, '2026-01-01', '充电中', 0, 0, 1, '2026-01-01 00:00:00', '')""",
                (pile_id, right),
            )


def test_legacy_duplicates_reconciled_on_init(client):
    """历史脏数据（同桩两笔持有）在 init_db 时收敛为一笔，且事件留痕。"""
    pile_id, left, right = _ids("河甲三号")
    with db(immediate=True) as conn:
        # 临时摘掉唯一索引再写入脏数据
        conn.execute("DROP INDEX IF EXISTS ux_sessions_pile_holding")
        conn.execute("DROP INDEX IF EXISTS ux_sessions_gun_holding")
        for i, gun in enumerate((left, right)):
            conn.execute(
                """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,
                                        kwh,amount,price_locked,started_at,note)
                   VALUES(?, 1, ?, ?, 1, '2026-01-01', '充电中', 0, 0, 1, '2026-01-01 00:00:00', '')""",
                (f"脏{i}", pile_id, gun),
            )
    init_db()
    with db() as conn:
        held = conn.execute(
            "SELECT id, slip_no FROM sessions WHERE pile_id=? AND status IN ('充电中','结算中','已超时') ORDER BY id",
            (pile_id,),
        ).fetchall()
        assert len(held) == 1
        assert held[0]["slip_no"] == "脏0"
        interrupted = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE slip_no='脏1' AND status='已中断'"
        ).fetchone()["n"]
        assert interrupted == 1
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM session_events WHERE action='校正中断'").fetchone()["n"]
            == 1
        )


def test_concurrent_conflict_does_not_consume_slip_number(client):
    """并发落败方整笔回滚时，会话序号不得被白占（无断号）。"""
    pile_id, left, right = _ids("河甲三号")
    for gun in (left, right):
        with db(immediate=True) as conn:
            res = svc.open_session(conn, pile_id, gun)
        if res["成功"]:
            first = res["会话号"]
    with db() as conn:
        seq = conn.execute("SELECT value FROM settings WHERE key='会话序号'").fetchone()["value"]
    assert first == "充1001"
    assert seq == "1001"
