from __future__ import annotations
import json
import threading

from conftest import open_pile, pile_by_name


def holding_sessions(client, pile_id: int):
    return [
        s
        for s in client.get("/api/会话").json()
        if s["pile_id"] == pile_id and s["status"] in ("充电中", "结算中", "已超时")
    ]


def test_dual_gun_second_open_rejected_wholesale(client):
    """一枪在充再开另一枪：第二笔整笔拒绝，且不改已有会话与占用态。"""
    pile = pile_by_name(client, "河甲三号")
    left, right = pile["枪线"]
    assert left["状态"] == right["状态"] == "可用"

    first = open_pile(client, pile["id"], left["id"])
    assert first.status_code == 200, first.text
    assert first.json()["桩位有效态"] == "占用"
    assert first.json()["枪线有效态"] == "占用"

    snapshot = client.get(f"/api/会话/{first.json()['会话编号']}").json()

    second = open_pile(client, pile["id"], right["id"])
    assert second.status_code == 400
    detail = second.json()["detail"]
    assert detail["成功"] is False
    assert "同桩互斥单充" in detail["规则"]
    assert first.json()["会话号"] in detail["拒因"]

    # 已有会话原样保留：状态、电量、开始时间都不动。
    after = client.get(f"/api/会话/{first.json()['会话编号']}").json()
    assert after["status"] == snapshot["status"] == "充电中"
    assert after["started_at"] == snapshot["started_at"]
    assert after["kwh"] == snapshot["kwh"]
    assert len(holding_sessions(client, pile["id"])) == 1

    # 桩位页双枪条必须解释占用：第一枪持有、第二枪因同桩互斥而占用。
    page = pile_by_name(client, "河甲三号")
    bar = {g["id"]: g for g in page["枪线"]}
    assert bar[left["id"]]["会话号"] == first.json()["会话号"]
    assert bar[right["id"]]["状态"] == "占用"
    assert "同桩互斥" in bar[right["id"]]["占因"]
    assert page["status"] == "占用"


def test_second_open_does_not_replace_first_session_and_is_audited(client):
    pile = pile_by_name(client, "河甲三号")
    left, right = pile["枪线"]
    first = open_pile(client, pile["id"], left["id"]).json()

    open_pile(client, pile["id"], right["id"])
    open_pile(client, pile["id"])  # 自动选枪也必须被拒

    audit = client.get(f"/api/开充审计?桩位编号={pile['id']}").json()
    rejects = [a for a in audit if a["result"] == "开充拒绝"]
    oks = [a for a in audit if a["result"] == "开充成功"]
    assert len(rejects) == 2
    assert len(oks) == 1
    assert oks[0]["slip_no"] == first["会话号"]
    assert all(a["session_id"] is None for a in rejects)

    sessions = [s for s in client.get("/api/会话").json() if s["pile_id"] == pile["id"]]
    assert [s["slip_no"] for s in sessions] == [first["会话号"]]


def test_receipt_ledger_guns_bar_overview_agree(client):
    pile = pile_by_name(client, "河甲三号")
    before = client.get("/api/总览").json()
    left = pile["枪线"][0]
    receipt = open_pile(client, pile["id"], left["id"]).json()

    assert receipt["总览"]["充电中"] == before["充电中"] + 1
    assert receipt["总览"]["桩位占用"] == before["桩位占用"] + 1
    assert receipt["总览"]["桩位空闲"] == before["桩位空闲"] - 1
    assert receipt["总览"]["持有桩位数"] == receipt["总览"]["桩位占用"] - (
        # 故障/维护不计入占用，这里只做一致性：占用桩必有持有会话
        0
    )

    # 会话册同一口径
    row = next(
        s for s in client.get("/api/会话").json() if s["id"] == receipt["会话编号"]
    )
    assert row["桩位有效态"] == "占用"
    assert row["枪线有效态"] == "占用"
    assert row["持有中"] is True

    # 枪线页同一口径
    guns = {g["id"]: g for g in client.get(f"/api/枪线?桩位编号={pile['id']}").json()}
    assert guns[left["id"]]["status"] == "占用"
    assert guns[left["id"]]["会话号"] == receipt["会话号"]

    # 回执双枪条与桩位页双枪条一致
    page_bar = {g["id"]: g["状态"] for g in pile_by_name(client, "河甲三号")["枪线"]}
    receipt_bar = {g["id"]: g["状态"] for g in receipt["双枪条"]}
    assert page_bar == receipt_bar


def test_settle_releases_pile_and_gun_then_other_gun_can_open(client):
    pile = pile_by_name(client, "河甲三号")
    left, right = pile["枪线"]
    first = open_pile(client, pile["id"], left["id"]).json()

    settled = client.post(f"/api/会话/{first['会话编号']}/结算").json()
    assert settled["桩位有效态"] == "空闲"
    assert settled["枪线有效态"] == "可用"
    assert all(g["状态"] == "可用" for g in settled["双枪条"])

    second = open_pile(client, pile["id"], right["id"])
    assert second.status_code == 200
    assert second.json()["枪线编号"] == right["id"]


def test_timeout_keeps_pile_occupied(client):
    pile = pile_by_name(client, "河甲三号")
    first = open_pile(client, pile["id"]).json()
    out = client.post(f"/api/会话/{first['会话编号']}/超时").json()
    assert out["状态"] == "已超时"
    assert out["桩位有效态"] == "占用"

    again = open_pile(client, pile["id"], pile["枪线"][1]["id"])
    assert again.status_code == 400

    # 超时会话可以关账，关账后释放。
    client.post(f"/api/会话/{first['会话编号']}/结算")
    assert pile_by_name(client, "河甲三号")["status"] == "空闲"


def test_concurrent_double_open_only_one_lands(client):
    """并发双开：即使两边同时通过预检，也只能落一笔会话。"""
    pile = pile_by_name(client, "河甲三号")
    left, right = pile["枪线"]
    results: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def attempt(gun_id):
        barrier.wait()
        r = open_pile(client, pile["id"], gun_id)
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=attempt, args=(g,)) for g in (left["id"], right["id"])]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 400], results
    assert len(holding_sessions(client, pile["id"])) == 1
    assert pile_by_name(client, "河甲三号")["status"] == "占用"


def test_half_open_rollback_leaves_no_wrong_gun_occupancy(client):
    """半截开充（并发冲突回滚）不得留下错的枪线占用。"""
    pile = pile_by_name(client, "河甲三号")
    left, right = pile["枪线"]
    first = open_pile(client, pile["id"], left["id"])
    assert first.status_code == 200

    attempt = open_pile(client, pile["id"], right["id"])
    assert attempt.status_code == 400

    page = pile_by_name(client, "河甲三号")
    bar = {g["id"]: g for g in page["枪线"]}
    # 右枪没有任何会话，不得显示为自己持有占用
    assert bar[right["id"]]["会话号"] is None
    assert bar[right["id"]]["状态"] == "占用"  # 因同桩互斥而占用
    assert "同桩互斥" in bar[right["id"]]["占因"]
    # 会话册里不存在右枪会话
    assert all(s["gun_id"] != right["id"] for s in client.get("/api/会话").json())


def test_precheck_is_replayable_and_matches_actual_open(client):
    """选枪谓词可重放：预检快照 + 请求枪号决定的结果与实际开充一致。"""
    pile = pile_by_name(client, "河甲三号")
    pre = client.get(f"/api/开会话/预检?桩位编号={pile['id']}").json()
    assert pre["可开充"] is True
    picked = pre["选中枪线"]

    receipt = open_pile(client, pile["id"]).json()
    assert receipt["枪线编号"] == picked["id"]

    pre2 = client.get(f"/api/开会话/预检?桩位编号={pile['id']}&枪线编号={pile['枪线'][1]['id']}").json()
    assert pre2["可开充"] is False
    assert pre2["持有会话"]["会话编号"] == receipt["会话编号"]

    # 候选快照完整保留在审计里，可离线重放同一谓词。
    audit = client.get(f"/api/开充审计?桩位编号={pile['id']}").json()
    ok = next(a for a in audit if a["result"] == "开充成功")
    assert ok["候选枪线"] == pre["候选枪线"]
    assert json.dumps(ok["候选枪线"], ensure_ascii=False)


def test_overview_counts_sessions_not_only_piles(client):
    pile = pile_by_name(client, "河甲三号")
    open_pile(client, pile["id"])
    o = client.get("/api/总览").json()
    assert o["并充口径"] == "同桩互斥单充"
    assert o["同桩并充上限"] == 1
    assert o["持有会话数"] == o["充电中"] + o["结算中"] + o["已超时"]
    assert o["持有桩位数"] == o["桩位占用"]
    assert o["在充电量"] == 0.0


def test_no_fake_parallel_light_on_receipts_or_overview(client):
    """假并枪收口：回执与总览不得携带粉刷层；第二枪被拒后总览仍说真话。"""
    pile = pile_by_name(client, "河甲三号")
    left, right = pile["枪线"]

    receipt = open_pile(client, pile["id"], left["id"]).json()
    assert "并枪" not in receipt
    assert "surface_ok" not in receipt
    assert receipt["总览"]["充电中"] == 1
    assert receipt["总览"]["桩位占用"] == 1

    second = open_pile(client, pile["id"], right["id"])
    assert second.status_code == 400
    detail = second.json()["detail"]
    assert "并枪" not in detail
    assert "surface_ok" not in detail

    # 拒绝不得粉刷总览：充电中桶不超过桩数，更不得大于持有会话数。
    o = client.get("/api/总览").json()
    assert not any(k.startswith("surface_") for k in o)
    pile_total = o["桩位空闲"] + o["桩位占用"] + o["桩位故障"] + o["桩位维护"]
    assert o["充电中"] == 1
    assert o["充电中"] <= o["持有桩位数"] <= pile_total

    # 会话册里同桩只有一行持有会话，不存在第二枪会话行。
    holding = [
        s
        for s in client.get("/api/会话").json()
        if s["pile_id"] == pile["id"] and s["持有中"]
    ]
    assert [s["slip_no"] for s in holding] == [receipt["会话号"]]


def test_abort_releases_both_guns_leaving_no_ghost_occupancy(client):
    """一枪急停：整桩同命运释放，另一枪不得残留幽灵占用（含存储态）。"""
    pile = pile_by_name(client, "河甲三号")
    left, right = pile["枪线"]
    receipt = open_pile(client, pile["id"], left["id"]).json()

    out = client.post(
        f"/api/会话/{receipt['会话编号']}/中断", json={"原因": "急停"}
    ).json()
    assert out["状态"] == "已中断"
    assert out["桩位有效态"] == "空闲"
    assert out["枪线有效态"] == "可用"
    assert all(g["状态"] == "可用" for g in out["双枪条"])

    page = pile_by_name(client, "河甲三号")
    assert page["status"] == "空闲"
    assert all(g["状态"] == "可用" and not g["占因"] for g in page["枪线"])

    # 枪线接口同一口径：有效态与存储态都不得有旁路写入的「占用」残留。
    guns = client.get(f"/api/枪线?桩位编号={pile['id']}").json()
    assert all(g["status"] == "可用" for g in guns)
    assert all(g["存储状态"] == "可用" for g in guns)

    o = client.get("/api/总览").json()
    assert o["充电中"] == 0
    assert o["桩位占用"] == 0
    assert o["已中断"] == 1
