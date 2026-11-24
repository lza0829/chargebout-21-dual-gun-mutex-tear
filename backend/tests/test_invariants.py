from __future__ import annotations
import threading

import pytest
from fastapi import HTTPException

from app.db import db, init_db
from app.services import core
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


def test_pulse_frozen_on_mutex_breach(client):
    """互斥破口（同桩两笔持有）上，脉冲写口必须冻结落账，不得继续记电量金额。"""
    pile_id, left, right = _ids("河甲三号")
    with db(immediate=True) as conn:
        sid = svc.open_session(conn, pile_id, left)["会话编号"]
    try:
        with db(immediate=True) as conn:
            # 摘掉兜底唯一索引，手工制造同桩双持有的互斥破口
            conn.execute("DROP INDEX IF EXISTS ux_sessions_pile_holding")
            conn.execute("DROP INDEX IF EXISTS ux_sessions_gun_holding")
            conn.execute(
                """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,
                                        kwh,amount,price_locked,started_at,note)
                   VALUES('破口', 1, ?, ?, 1, '2026-01-01', '充电中', 0, 0, 1, '2026-01-01 00:00:00', '')""",
                (pile_id, right),
            )
        with db() as conn:
            # 破口本身对占用推导可见（可识破），但写口不得在其上落账
            assert core.holding_count(conn, pile_id) == 2
        with pytest.raises(HTTPException) as exc:
            with db(immediate=True) as conn:
                svc.add_pulse(conn, sid, 1.5, 7)
        assert exc.value.status_code == 409
        with db() as conn:
            se = conn.execute("SELECT kwh, amount FROM sessions WHERE id=?", (sid,)).fetchone()
            assert se["kwh"] == 0
            assert se["amount"] == 0
            assert (
                conn.execute("SELECT COUNT(*) AS n FROM pulses WHERE session_id=?", (sid,)).fetchone()["n"]
                == 0
            )
    finally:
        # 重建唯一索引并收敛破口（校正中断留痕），避免污染后续用例
        init_db()


def test_fake_surface_settings_purged_on_init(client):
    """撕裂层残留键（粉刷总览/旁路并枪痕迹）在 init_db 时一律清创。"""
    with db(immediate=True) as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('surface_last','{\"ok\": true, \"充电中\": 2}')"
        )
        conn.execute("INSERT INTO settings(key,value) VALUES('surface_ok','1')")
        conn.execute("INSERT INTO settings(key,value) VALUES('mutex_twin:3','{\"primary\": 1}')")
        conn.execute("UPDATE guns SET status='占用'")
    init_db()
    with db() as conn:
        leftover = conn.execute(
            "SELECT COUNT(*) AS n FROM settings WHERE key IN ('surface_last','surface_ok') OR key LIKE 'mutex_twin:%'"
        ).fetchone()["n"]
        assert leftover == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM guns WHERE status='占用'").fetchone()["n"] == 0
        # 清创不得误伤正常设置项
        assert conn.execute("SELECT value FROM settings WHERE key='会话序号'").fetchone()["value"] == "1000"
