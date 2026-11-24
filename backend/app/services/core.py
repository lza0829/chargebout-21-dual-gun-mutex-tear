from __future__ import annotations
import json
import sqlite3
from datetime import datetime
from app.services import mutex_surface

from app.config import (
    GUN_PICK_RULE,
    HOLDING_SESSIONS,
    METERED_SESSIONS,
    OPEN_POLICY,
    PILE_CONCURRENCY_LIMIT,
)
from app.db import row_dict, rows

_HOLD_SQL = "(" + ",".join("'" + s + "'" for s in HOLDING_SESSIONS) + ")"
_METER_SQL = "(" + ",".join("'" + s + "'" for s in METERED_SESSIONS) + ")"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_setting(conn, key: str, default: str = "") -> str:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def current_band_price(conn, rate_plan_id: int) -> float:
    plan = conn.execute("SELECT * FROM rate_plans WHERE id=?", (rate_plan_id,)).fetchone()
    if not plan:
        return 0.0
    hour = datetime.now().hour
    if 8 <= hour < 11 or 18 <= hour < 21:
        return float(plan["peak_price"])
    if 23 <= hour or hour < 7:
        return float(plan["valley_price"])
    return float(plan["flat_price"])


def is_day_sealed(conn, day: str | None = None) -> bool:
    d = day or today()
    r = conn.execute("SELECT sealed FROM day_seals WHERE business_day=?", (d,)).fetchone()
    return bool(r and r["sealed"])


# ---------------------------------------------------------------------------
# 占用态唯一推导口
#
# 全局口径「同桩互斥单充」：
#   同一桩位任意时刻至多一笔持有中的会话；持有 = 充电中 / 结算中 / 已超时。
#   桩位占用、枪线占用、开会话可用谓词、桩位页双枪条、会话册、总览、审计回放
#   全部从这里推导，不各自维护一套判定。
# ---------------------------------------------------------------------------

def holding_session(conn, *, pile_id: int | None = None, gun_id: int | None = None):
    """桩位/枪线当前持有的那笔会话；互斥单充下至多一笔。"""
    clauses, params = [f"status IN {_HOLD_SQL}"], []
    if pile_id is not None:
        clauses.append("pile_id=?")
        params.append(pile_id)
    if gun_id is not None:
        clauses.append("gun_id=?")
        params.append(gun_id)
    return conn.execute(
        f"SELECT * FROM sessions WHERE {' AND '.join(clauses)} ORDER BY id LIMIT 1",
        params,
    ).fetchone()


def _holding_maps(conn) -> tuple[dict[int, sqlite3.Row], dict[int, sqlite3.Row]]:
    """一次取出全部持有会话，按桩/按枪建索引，供批量推导占用态（避免 N+1）。"""
    by_pile: dict[int, sqlite3.Row] = {}
    by_gun: dict[int, sqlite3.Row] = {}
    for r in conn.execute(
        f"SELECT * FROM sessions WHERE status IN {_HOLD_SQL} ORDER BY id"
    ).fetchall():
        by_pile.setdefault(r["pile_id"], r)
        by_gun.setdefault(r["gun_id"], r)
    return by_pile, by_gun


def pile_effective_status(conn, pile) -> str:
    """桩位有效态：手工故障/维护优先，否则由持有会话推导。"""
    if pile["status"] in ("故障", "维护"):
        return pile["status"]
    return "占用" if holding_session(conn, pile_id=pile["id"]) else "空闲"


def gun_hold_reason(conn, gun) -> str:
    """枪线不可用的原因；空串表示可用。"""
    if gun["status"] == "故障":
        return "枪线故障"
    held = holding_session(conn, gun_id=gun["id"])
    if held:
        return f"本枪持有会话 {held['slip_no']}"
    pile_held = holding_session(conn, pile_id=gun["pile_id"])
    if pile_held:
        return f"同桩互斥：桩内 {pile_held['slip_no']} 在充，另一枪不可并充"
    return ""


def gun_effective_status(conn, gun) -> str:
    """枪线有效态：故障 / 占用 / 可用。同桩互斥时另一枪也记为占用。"""
    if gun["status"] == "故障":
        return "故障"
    return "占用" if gun_hold_reason(conn, gun) else "可用"


def _gun_bar_from(guns, by_pile, by_gun) -> list[dict]:
    out = []
    for g in guns:
        held = by_gun.get(g["id"])
        if g["status"] == "故障":
            reason, status = "枪线故障", "故障"
        elif held:
            reason, status = f"本枪持有会话 {held['slip_no']}", "占用"
        elif by_pile.get(g["pile_id"]):
            reason = f"同桩互斥：桩内 {by_pile[g['pile_id']]['slip_no']} 在充，另一枪不可并充"
            status = "占用"
        else:
            reason, status = "", "可用"
        out.append(
            {
                "id": g["id"],
                "name": g["name"],
                "状态": status,
                "占因": reason,
                "手工态": g["status"],
                "会话编号": held["id"] if held else None,
                "会话号": held["slip_no"] if held else None,
                "会话状态": held["status"] if held else None,
            }
        )
    return out


def gun_bar(conn, pile_id: int) -> list[dict]:
    """桩位页双枪条：每把枪的有效态、占因与所持会话。"""
    by_pile, by_gun = _holding_maps(conn)
    guns = conn.execute("SELECT * FROM guns WHERE pile_id=? ORDER BY id", (pile_id,)).fetchall()
    return _gun_bar_from(guns, by_pile, by_gun)


def _pile_public(conn, p, by_pile, by_gun, guns_by_pile) -> dict:
    held = by_pile.get(p["id"])
    row = dict(p)
    row["存储状态"] = p["status"]
    if p["status"] in ("故障", "维护"):
        row["status"] = p["status"]
    else:
        row["status"] = "占用" if held else "空闲"
    row["枪线"] = _gun_bar_from(guns_by_pile.get(p["id"], []), by_pile, by_gun)
    row["在充会话"] = (
        {
            "会话编号": held["id"],
            "会话号": held["slip_no"],
            "状态": held["status"],
            "枪线编号": held["gun_id"],
            "电量": held["kwh"],
            "锁定电价": held["price_locked"],
            "开始时间": held["started_at"],
        }
        if held
        else None
    )
    return row


def overview(conn) -> dict:
    by_pile, _ = _holding_maps(conn)
    pile_map: dict[str, int] = {}
    for p in conn.execute("SELECT * FROM piles ORDER BY id").fetchall():
        if p["status"] in ("故障", "维护"):
            eff = p["status"]
        else:
            eff = "占用" if p["id"] in by_pile else "空闲"
        pile_map[eff] = pile_map.get(eff, 0) + 1
    sess = rows(conn.execute("SELECT status, COUNT(*) AS n FROM sessions GROUP BY status").fetchall())
    sess_map = {s["status"]: s["n"] for s in sess}
    open_kwh = conn.execute(
        f"SELECT COALESCE(SUM(kwh),0) AS k FROM sessions WHERE status IN {_METER_SQL}"
    ).fetchone()["k"]
    holding_n = conn.execute(
        f"SELECT COUNT(*) AS n FROM sessions WHERE status IN {_HOLD_SQL}"
    ).fetchone()["n"]
    holding_piles = conn.execute(
        f"SELECT COUNT(DISTINCT pile_id) AS n FROM sessions WHERE status IN {_HOLD_SQL}"
    ).fetchone()["n"]
    today_amount = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS a FROM sessions WHERE business_day=? AND status='已关账'",
        (today(),),
    ).fetchone()["a"]
    return {
        "桩位空闲": pile_map.get("空闲", 0),
        "桩位占用": pile_map.get("占用", 0),
        "桩位故障": pile_map.get("故障", 0),
        "桩位维护": pile_map.get("维护", 0),
        "充电中": sess_map.get("充电中", 0),
        "结算中": sess_map.get("结算中", 0),
        "已关账": sess_map.get("已关账", 0),
        "已超时": sess_map.get("已超时", 0),
        "已中断": sess_map.get("已中断", 0),
        "持有会话数": holding_n,
        "持有桩位数": holding_piles,
        "在充电量": round(float(open_kwh), 3),
        "本日关账金额": round(float(today_amount), 2),
        "营业日": today(),
        "充日已封印": is_day_sealed(conn),
        "并充口径": OPEN_POLICY,
        "同桩并充上限": PILE_CONCURRENCY_LIMIT,
        "选枪谓词": GUN_PICK_RULE,
    }


def list_sites(conn):
    return rows(conn.execute("SELECT * FROM sites ORDER BY id").fetchall())


def list_piles(conn, site_id: int | None = None):
    if site_id:
        rs = conn.execute(
            """SELECT p.*, s.name AS site_name, r.name AS rate_name
               FROM piles p JOIN sites s ON s.id=p.site_id
               JOIN rate_plans r ON r.id=p.rate_plan_id
               WHERE p.site_id=? ORDER BY p.id""",
            (site_id,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT p.*, s.name AS site_name, r.name AS rate_name
               FROM piles p JOIN sites s ON s.id=p.site_id
               JOIN rate_plans r ON r.id=p.rate_plan_id
               ORDER BY p.id"""
        ).fetchall()
    by_pile, by_gun = _holding_maps(conn)
    guns_by_pile: dict[int, list] = {}
    for g in conn.execute("SELECT * FROM guns ORDER BY id").fetchall():
        guns_by_pile.setdefault(g["pile_id"], []).append(g)
    return [_pile_public(conn, row_dict(r), by_pile, by_gun, guns_by_pile) for r in rs]


def list_guns(conn, pile_id: int | None = None):
    if pile_id:
        rs = conn.execute(
            "SELECT g.*, p.name AS pile_name FROM guns g JOIN piles p ON p.id=g.pile_id WHERE g.pile_id=? ORDER BY g.id",
            (pile_id,),
        ).fetchall()
    else:
        rs = conn.execute(
            "SELECT g.*, p.name AS pile_name FROM guns g JOIN piles p ON p.id=g.pile_id ORDER BY g.id"
        ).fetchall()
    out = []
    for r in rs:
        d = row_dict(r)
        d["存储状态"] = d["status"]
        d["status"] = gun_effective_status(conn, r)
        d["占因"] = gun_hold_reason(conn, r)
        held = holding_session(conn, gun_id=r["id"])
        d["会话编号"] = held["id"] if held else None
        d["会话号"] = held["slip_no"] if held else None
        out.append(d)
    return out


def list_rates(conn):
    return rows(conn.execute("SELECT * FROM rate_plans ORDER BY id").fetchall())


def list_sessions(conn, status: str | None = None):
    if status:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               WHERE se.status=? ORDER BY se.id DESC""",
            (status,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               ORDER BY se.id DESC LIMIT 200"""
        ).fetchall()
    out = []
    for r in rs:
        d = row_dict(r)
        pile = conn.execute("SELECT * FROM piles WHERE id=?", (r["pile_id"],)).fetchone()
        gun = conn.execute("SELECT * FROM guns WHERE id=?", (r["gun_id"],)).fetchone()
        d["桩位有效态"] = pile_effective_status(conn, pile) if pile else "—"
        d["枪线有效态"] = gun_effective_status(conn, gun) if gun else "—"
        d["持有中"] = r["status"] in HOLDING_SESSIONS
        out.append(d)
    return out


def session_detail(conn, session_id: int):
    se = conn.execute(
        """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name, r.name AS rate_name
           FROM sessions se
           JOIN piles p ON p.id=se.pile_id
           JOIN guns g ON g.id=se.gun_id
           JOIN sites s ON s.id=se.site_id
           JOIN rate_plans r ON r.id=se.rate_plan_id
           WHERE se.id=?""",
        (session_id,),
    ).fetchone()
    if not se:
        return None
    pulses = rows(
        conn.execute(
            "SELECT * FROM pulses WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    events = rows(
        conn.execute(
            "SELECT * FROM session_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    d = row_dict(se)
    d["脉冲"] = pulses
    d["事件"] = events
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (se["pile_id"],)).fetchone()
    gun = conn.execute("SELECT * FROM guns WHERE id=?", (se["gun_id"],)).fetchone()
    d["桩位有效态"] = pile_effective_status(conn, pile) if pile else "—"
    d["枪线有效态"] = gun_effective_status(conn, gun) if gun else "—"
    d["并充口径"] = OPEN_POLICY
    return d


def list_open_audit(conn, pile_id: int | None = None, limit: int = 200) -> list[dict]:
    if pile_id:
        rs = conn.execute(
            "SELECT * FROM open_audit WHERE pile_id=? ORDER BY id DESC LIMIT ?",
            (pile_id, limit),
        ).fetchall()
    else:
        rs = conn.execute("SELECT * FROM open_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rs:
        d = row_dict(r)
        try:
            d["候选枪线"] = json.loads(d.pop("candidates") or "[]")
        except json.JSONDecodeError:
            d["候选枪线"] = []
        out.append(d)
    return out

_overview_impl = overview


def overview(conn):
    return mutex_surface.overlay_overview(_overview_impl(conn), conn)

