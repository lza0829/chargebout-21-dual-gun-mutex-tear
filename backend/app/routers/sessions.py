from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import db
from app.services import core
from app.services import sessions as svc

router = APIRouter()


class OpenIn(BaseModel):
    桩位编号: int
    枪线编号: int | None = None


class PulseIn(BaseModel):
    电量: float = Field(gt=0)
    功率: float = 0


class AbortIn(BaseModel):
    原因: str = "急停"


@router.get("/会话")
def list_sessions(状态: str | None = None):
    with db() as conn:
        return core.list_sessions(conn, 状态)


@router.get("/会话/{session_id}")
def get_session(session_id: int):
    with db() as conn:
        d = core.session_detail(conn, session_id)
        if not d:
            raise HTTPException(404, detail="会话不存在")
        return d


@router.get("/开会话/预检")
def precheck(桩位编号: int, 枪线编号: int | None = None):
    """只读重放：与真开充共用同一谓词实现，不落任何状态。"""
    with db() as conn:
        pile = conn.execute("SELECT * FROM piles WHERE id=?", (桩位编号,)).fetchone()
        if not pile:
            raise HTTPException(404, detail="桩位不存在")
        candidates = svc.gun_candidates(conn, 桩位编号)
        held = core.holding_session(conn, pile_id=桩位编号)
        verdict = svc.evaluate_open(conn, pile, 枪线编号, candidates, held)
        return {
            "桩位编号": 桩位编号,
            "桩位有效态": core.pile_effective_status(conn, pile),
            "持有会话": (
                {"会话编号": held["id"], "会话号": held["slip_no"], "状态": held["status"], "枪线编号": held["gun_id"]}
                if held
                else None
            ),
            "候选枪线": candidates,
            "选中枪线": verdict["选中枪线"],
            "可开充": verdict["可开充"],
            "拒因": verdict["拒因"],
            "规则": verdict["规则"],
            "并充口径": core.OPEN_POLICY,
            "选枪谓词": core.GUN_PICK_RULE,
        }


@router.post("/开会话")
def open_session(body: OpenIn):
    with db(immediate=True) as conn:
        result = svc.open_session(conn, body.桩位编号, body.枪线编号)
    # 拒绝也要留下审计流水：先提交事务，再在此抛错。
    if not result["成功"]:
        raise HTTPException(result["状态码"], detail=result)
    return result


@router.get("/开充审计")
def list_open_audit(桩位编号: int | None = None):
    with db() as conn:
        return core.list_open_audit(conn, 桩位编号)


@router.post("/会话/{session_id}/脉冲")
def add_pulse(session_id: int, body: PulseIn):
    with db(immediate=True) as conn:
        return svc.add_pulse(conn, session_id, body.电量, body.功率)


@router.post("/会话/{session_id}/结算")
def settle(session_id: int):
    with db(immediate=True) as conn:
        return svc.settle(conn, session_id)


@router.post("/会话/{session_id}/超时")
def timeout(session_id: int):
    with db(immediate=True) as conn:
        return svc.mark_timeout(conn, session_id)


@router.post("/会话/{session_id}/中断")
def abort(session_id: int, body: AbortIn | None = None):
    with db(immediate=True) as conn:
        reason = body.原因 if body else "急停"
        return svc.abort_session(conn, session_id, reason)

