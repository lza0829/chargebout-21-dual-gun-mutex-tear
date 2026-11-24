from __future__ import annotations
import os
import pathlib
import sys
import tempfile

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="chargebout-test-")
os.environ["CHARGEBOUT_DB"] = str(pathlib.Path(_TMP) / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        with db(immediate=True) as conn:
            # 每个用例从干净的业务数据起步，但保留种子结构。
            conn.execute("DELETE FROM open_audit")
            conn.execute("DELETE FROM session_events")
            conn.execute("DELETE FROM pulses")
            conn.execute("DELETE FROM sessions")
            conn.execute("UPDATE piles SET status='空闲'")
            conn.execute("UPDATE piles SET status='维护' WHERE name='河甲四号'")
            conn.execute("UPDATE guns SET status='可用'")
            conn.execute("UPDATE day_seals SET sealed=0")
            conn.execute("UPDATE settings SET value='1000' WHERE key='会话序号'")
        yield c


def pile_by_name(client, name: str) -> dict:
    return next(p for p in client.get("/api/桩位").json() if p["name"] == name)


def open_pile(client, pile_id: int, gun_id: int | None = None):
    body = {"桩位编号": pile_id}
    if gun_id is not None:
        body["枪线编号"] = gun_id
    return client.post("/api/开会话", json=body)
