from __future__ import annotations
import json
import sqlite3
from app.services import mutex_surface

from fastapi import HTTPException

from app.config import (
    GUN_PICK_RULE,
    HOLDING_SESSIONS,
    OPEN_POLICY,
    PILE_CONCURRENCY_LIMIT,
)
from app.services import core
from app.services.core import (
    current_band_price,
    get_setting,
    gun_effective_status,
    gun_hold_reason,
    holding_session,
    is_day_sealed,
    now_str,
    pile_effective_status,
    today,
)


def _next_slip(conn) -> str:
    n = int(get_setting(conn, "会话序号", "1000")) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('会话序号',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(n),),
    )
    return f"充{n}"


def _audit(conn, *, pile_id, gun_id, session_id, slip_no, result, reason, rule, candidates) -> int:
    cur = conn.execute(
        """INSERT INTO open_audit(pile_id,gun_id,session_id,slip_no,result,reason,rule,candidates,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            pile_id,
            gun_id,
            session_id,
            slip_no,
            result,
            reason,
            rule,
            json.dumps(candidates, ensure_ascii=False),
            now_str(),
        ),
    )
    return int(cur.lastrowid)


def gun_candidates(conn, pile_id: int) -> list[dict]:
    """选枪谓词的输入快照：同一快照喂给 pick_gun 必然得到同一结果（可重放）。"""
    out = []
    for g in conn.execute("SELECT * FROM guns WHERE pile_id=? ORDER BY id", (pile_id,)).fetchall():
        held = holding_session(conn, gun_id=g["id"])
        out.append(
            {
                "id": g["id"],
                "name": g["name"],
                "手工态": g["status"],
                "有效态": gun_effective_status(conn, g),
                "占因": gun_hold_reason(conn, g),
                "持有会话": held["slip_no"] if held else None,
            }
        )
    return out


def pick_gun(candidates: list[dict], requested_gun_id: int | None) -> tuple[dict | None, str]:
    """纯谓词：只依赖候选快照与请求枪号，不读库，因此可离线重放。

    规则：指定枪号则须属于本桩且有效态可用；不指定则按 id 升序取首个可用。
    返回 (选中的枪, 拒因)；拒因为空串表示选中。
    """
    if requested_gun_id is not None:
        gun = next((c for c in candidates if c["id"] == requested_gun_id), None)
        if gun is None:
            return None, "所选枪线不属于该桩位"
        if gun["有效态"] != "可用":
            return None, gun["占因"] or f"所选枪线当前为{gun['有效态']}"
        return gun, ""
    for c in candidates:
        if c["有效态"] == "可用":
            return c, ""
    return None, "本桩无可用枪线"


def _reject(conn, *, pile_id, gun_id, reason, rule, candidates, code=400) -> dict:
    audit_id = _audit(
        conn,
        pile_id=pile_id,
        gun_id=gun_id,
        session_id=None,
        slip_no=None,
        result="开充拒绝",
        reason=reason,
        rule=rule,
        candidates=candidates,
    )
    return {"成功": False, "状态码": code, "拒因": reason, "规则": rule, "审计编号": audit_id}


def evaluate_open(conn, pile, gun_id: int | None, candidates: list[dict], held) -> dict:
    """开充谓词的唯一实现：预检与真开充共用，保证「同输入同结论」可重放。

    只读，不落任何状态；`candidates` 与 `held` 由调用方用同一快照传入。
    """
    def no(reason: str, rule: str) -> dict:
        return {"可开充": False, "拒因": reason, "规则": rule, "选中枪线": None}

    if gun_id is not None and not any(c["id"] == gun_id for c in candidates):
        return no("所选枪线不属于该桩位", "枪线归属")
    if is_day_sealed(conn):
        return no("充日已封印，禁止新开充", "充日封印")
    site = conn.execute("SELECT * FROM sites WHERE id=?", (pile["site_id"],)).fetchone()
    if not site or not site["active"]:
        return no("站点未启用", "站点启用")
    if pile["status"] in ("故障", "维护"):
        return no(f"桩位当前为{pile['status']}，不可开充", "桩位手工态")

    # 互斥单充核心闸门：桩内已有任何持有会话即整笔拒绝，且绝不改已有会话。
    if held:
        requested = next((c for c in candidates if c["id"] == gun_id), None) if gun_id is not None else None
        if requested is not None and held["gun_id"] == requested["id"]:
            return no(f"该枪线已持有会话 {held['slip_no']}，不可重复开充", "同桩互斥单充")
        return no(
            f"{OPEN_POLICY}：本桩已有在充会话 {held['slip_no']}"
            f"（{held['status']}，上限 {PILE_CONCURRENCY_LIMIT} 笔），另一枪不可并充",
            "同桩互斥单充",
        )

    gun, why = pick_gun(candidates, gun_id)
    if gun is None:
        return no(why, GUN_PICK_RULE)
    return {"可开充": True, "拒因": "", "规则": GUN_PICK_RULE, "选中枪线": gun}


def open_session(conn, pile_id: int, gun_id: int | None = None) -> dict:
    """开充唯一入口。

    以「同桩互斥单充」为准：同一桩位任意时刻至多一笔持有会话。
    第二枪开充整笔拒绝且不触碰已有会话；本函数返回结果对象，由路由决定抛错，
    因此拒绝流水也能随事务提交（半截开充不会留下任何会话/占用残留）。
    """
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        return _reject(conn, pile_id=pile_id, gun_id=gun_id, reason="桩位不存在", rule="桩位存在性", candidates=[], code=404)

    candidates = gun_candidates(conn, pile_id)
    held = holding_session(conn, pile_id=pile_id)
    verdict = evaluate_open(conn, pile, gun_id, candidates, held)
    if not verdict["可开充"]:
        return _reject(
            conn,
            pile_id=pile_id,
            gun_id=gun_id,
            reason=verdict["拒因"],
            rule=verdict["规则"],
            candidates=candidates,
        )

    gun = verdict["选中枪线"]
    price = current_band_price(conn, pile["rate_plan_id"])
    ts = now_str()
    # 会话序号与插入放进同一保存点：并发冲突回滚时序号一并回滚，不留断号。
    conn.execute("SAVEPOINT open_session")
    try:
        slip = _next_slip(conn)
        cur = conn.execute(
            """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,kwh,amount,price_locked,started_at,note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                slip,
                pile["site_id"],
                pile_id,
                gun["id"],
                pile["rate_plan_id"],
                today(),
                "充电中",
                0,
                0,
                price,
                ts,
                "",
            ),
        )
        conn.execute("RELEASE SAVEPOINT open_session")
    except sqlite3.IntegrityError:
        # 兜底：并发双开时唯一索引只放行一笔，另一笔在此整笔回滚，不写会话、不占号。
        conn.execute("ROLLBACK TO SAVEPOINT open_session")
        conn.execute("RELEASE SAVEPOINT open_session")
        return _reject(
            conn,
            pile_id=pile_id,
            gun_id=gun["id"],
            reason=f"{OPEN_POLICY}：并发开充冲突，本桩已由另一笔占住，本次整笔拒绝",
            rule="同桩互斥唯一索引",
            candidates=candidates,
            code=409,
        )

    sid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (sid, "开会话", ts, f"锁定电价{price}；枪线{gun['name']}；{OPEN_POLICY}"),
    )
    audit_id = _audit(
        conn,
        pile_id=pile_id,
        gun_id=gun["id"],
        session_id=sid,
        slip_no=slip,
        result="开充成功",
        reason="",
        rule=GUN_PICK_RULE,
        candidates=candidates,
    )

    pile_row = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    receipt = {
        "成功": True,
        "状态码": 200,
        "会话号": slip,
        "会话编号": sid,
        "锁定电价": price,
        "状态": "充电中",
        "桩位编号": pile_id,
        "枪线编号": gun["id"],
        "枪线名称": gun["name"],
        "桩位有效态": pile_effective_status(conn, pile_row),
        "枪线有效态": gun_effective_status(
            conn, conn.execute("SELECT * FROM guns WHERE id=?", (gun["id"],)).fetchone()
        ),
        "双枪条": core.gun_bar(conn, pile_id),
        "并充口径": OPEN_POLICY,
        "选枪谓词": GUN_PICK_RULE,
        "审计编号": audit_id,
        "总览": core.overview(conn),
    }
    return receipt


def add_pulse(conn, session_id: int, delta_kwh: float, power_kw: float = 0) -> dict:
    if delta_kwh <= 0:
        raise HTTPException(400, detail="脉冲电量须大于零")
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可记脉冲")
    ts = now_str()
    conn.execute(
        "INSERT INTO pulses(session_id,delta_kwh,power_kw,created_at) VALUES(?,?,?,?)",
        (session_id, delta_kwh, power_kw, ts),
    )
    new_kwh = float(se["kwh"]) + float(delta_kwh)
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(new_kwh * price, 2)
    conn.execute(
        "UPDATE sessions SET kwh=?, amount=? WHERE id=?",
        (round(new_kwh, 3), amount, session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "脉冲", ts, f"+{delta_kwh}"),
    )
    return {
        "会话编号": session_id,
        "累计电量": round(new_kwh, 3),
        "预估金额": amount,
        "桩位有效态": _pile_state_after(conn, se["pile_id"]),
        "枪线有效态": _gun_state_after(conn, se["gun_id"]),
        "在充电量": round(float(core.overview(conn)["在充电量"]), 3),
    }


def _pile_state_after(conn, pile_id: int) -> str:
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    return pile_effective_status(conn, pile) if pile else "—"


def _gun_state_after(conn, gun_id: int) -> str:
    gun = conn.execute("SELECT * FROM guns WHERE id=?", (gun_id,)).fetchone()
    return gun_effective_status(conn, gun) if gun else "—"


def settle(conn, session_id: int) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in HOLDING_SESSIONS:
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可关账")
    ts = now_str()
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(float(se["kwh"]) * price, 2)
    conn.execute(
        "UPDATE sessions SET status='已关账', amount=?, closed_at=?, price_locked=? WHERE id=?",
        (amount, ts, price, session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "结算关账", ts, f"金额{amount}；释放桩位与枪线（占用态由会话推导）"),
    )
    return {
        "会话编号": session_id,
        "电量": float(se["kwh"]),
        "金额": amount,
        "状态": "已关账",
        "桩位有效态": _pile_state_after(conn, se["pile_id"]),
        "枪线有效态": _gun_state_after(conn, se["gun_id"]),
        "双枪条": core.gun_bar(conn, se["pile_id"]),
        "总览": core.overview(conn),
    }


def mark_timeout(conn, session_id: int) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可标超时")
    minutes = int(get_setting(conn, "超时分钟", "120"))
    ts = now_str()
    # 已超时仍持有桩位/枪线（未结），避免桩位被另一枪悄悄抢占。
    conn.execute(
        "UPDATE sessions SET status='已超时', note=? WHERE id=?",
        (f"超过{minutes}分钟未关，仍持有桩位待结算", session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "超时", ts, f"阈值{minutes}分钟；桩位继续占用"),
    )
    return {
        "会话编号": session_id,
        "状态": "已超时",
        "桩位有效态": _pile_state_after(conn, se["pile_id"]),
        "枪线有效态": _gun_state_after(conn, se["gun_id"]),
    }


def abort_session(conn, session_id: int, reason: str = "急停") -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in HOLDING_SESSIONS:
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可中断")
    ts = now_str()
    conn.execute(
        "UPDATE sessions SET status='已中断', closed_at=?, note=? WHERE id=?",
        (ts, reason, session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "异常中断", ts, reason),
    )
    return {
        "会话编号": session_id,
        "状态": "已中断",
        "原因": reason,
        "桩位有效态": _pile_state_after(conn, se["pile_id"]),
        "枪线有效态": _gun_state_after(conn, se["gun_id"]),
        "双枪条": core.gun_bar(conn, se["pile_id"]),
        "总览": core.overview(conn),
    }


def set_pile_status(conn, pile_id: int, status: str) -> dict:
    if status not in ("空闲", "故障", "维护"):
        if status == "占用":
            raise HTTPException(400, detail="占用态由在充会话推导，不可手工置为占用")
        raise HTTPException(400, detail="非法桩位状态")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    held = holding_session(conn, pile_id=pile_id)
    if held and status in ("故障", "维护"):
        raise HTTPException(400, detail=f"仍有持有会话 {held['slip_no']}，不可直接改故障或维护")
    if held and status == "空闲":
        raise HTTPException(400, detail=f"仍有持有会话 {held['slip_no']}，不可改空闲")
    conn.execute("UPDATE piles SET status=? WHERE id=?", (status, pile_id))
    return {"桩位编号": pile_id, "存储状态": status, "有效态": pile_effective_status(conn, conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone())}

_open_session_impl = open_session


def open_session(*args, **kwargs):
    out = _open_session_impl(*args, **kwargs)
    return mutex_surface.after_open(args[0] if args else None, out, *args[1:], **kwargs)

