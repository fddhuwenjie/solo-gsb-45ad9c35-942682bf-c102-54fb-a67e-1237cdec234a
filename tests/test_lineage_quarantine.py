"""按移液谱系扩展的孔位隔离：闭包传播、路径稳定性、原子拒绝与后续限制。"""

import os
import sqlite3


def register(client, well, sample, volume):
    resp = client.post(
        "/wells", json={"well": well, "sample": sample, "volume": volume}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def batch(client, request_key, operations, reservation_id=None):
    body = {"request_key": request_key, "operations": operations}
    if reservation_id is not None:
        body["reservation_id"] = reservation_id
    resp = client.post("/batches", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["batch_id"]


def lineage_quarantine(client, seed_wells, case_id="CASE-1", reason="链式污染排查",
                       request_key="lq-1"):
    return client.post(
        "/quarantines/lineage",
        json={
            "request_key": request_key,
            "case_id": case_id,
            "reason": reason,
            "seed_wells": seed_wells,
        },
    )


def quarantine(client, wells, case_id="CASE-1", request_key="q-1"):
    return client.post(
        "/quarantines",
        json={"request_key": request_key, "case_id": case_id,
              "reason": "污染排查", "wells": wells},
    )


def release(client, wells, case_id="CASE-1", request_key="rel-1"):
    return client.post(
        "/quarantines/releases",
        json={"request_key": request_key, "case_id": case_id, "wells": wells},
    )


def db_count(sql):
    conn = sqlite3.connect(os.environ["LINEAGE_DB"])
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _build_chain(client):
    """A1 -> B1 -> C1 -> D1，另 A2 -> E1（与 A1 谱系无关）。"""
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    batch(client, "b1", [
        {"source": "A1", "destination": "B1", "volume": "10"},
        {"source": "A2", "destination": "E1", "volume": "10"},
    ])
    batch(client, "b2", [{"source": "B1", "destination": "C1", "volume": "5"}])
    batch(client, "b3", [{"source": "C1", "destination": "D1", "volume": "3"}])


# ------------------------------------------------ 闭包传播与路径


def test_lineage_quarantine_propagates_along_committed_edges(client):
    _build_chain(client)
    resp = lineage_quarantine(client, ["A1"], request_key="lq-chain")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["propagation"] == "pipetting_lineage"
    assert body["seed_wells"] == ["A1"]
    assert body["replayed"] is False

    wells = {w["well"]: w for w in body["wells"]}
    # 种子及全部后代；A2/E1 不在 A1 的下游，故不进入闭包
    assert sorted(wells) == ["A1", "B1", "C1", "D1"]
    assert wells["A1"]["distance"] == 0
    assert wells["A1"]["path"] == ["A1"]
    assert wells["B1"]["distance"] == 1
    assert wells["B1"]["path"] == ["A1", "B1"]
    assert wells["C1"]["path"] == ["A1", "B1", "C1"]
    assert wells["D1"]["path"] == ["A1", "B1", "C1", "D1"]
    assert all(w["status"] == "quarantined" for w in body["wells"])

    # 各孔查询都呈现该隔离（后续限制沿用普通隔离）
    for well in ("A1", "B1", "C1", "D1"):
        assert client.get(f"/wells/{well}").json()["quarantine"]["case_id"] == "CASE-1"
    assert client.get("/wells/A2").json()["quarantine"] is None
    assert client.get("/wells/E1").json()["quarantine"] is None


def test_shortest_path_preferred_over_longer(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    batch(client, "b1", [{"source": "A1", "destination": "B1", "volume": "5"}])
    batch(client, "b2", [{"source": "B1", "destination": "C1", "volume": "3"}])
    # 之后新增直达边 A1 -> C1：更短路径应胜出
    batch(client, "b3", [{"source": "A1", "destination": "C1", "volume": "1"}])
    resp = lineage_quarantine(client, ["A1"])
    path = {w["well"]: w["path"] for w in resp.json()["wells"]}["C1"]
    assert path == ["A1", "C1"]


def test_equal_length_paths_lexicographically_stable(client):
    register(client, "A1", "S1", "100")
    batch(client, "b1", [
        {"source": "A1", "destination": "C1", "volume": "5"},
        {"source": "A1", "destination": "B1", "volume": "5"},
    ])
    # 无论边插入顺序如何，B1、C1 同长时路径按节点序列字典序取最小
    batch(client, "b2", [
        {"source": "C1", "destination": "D1", "volume": "2"},
        {"source": "B1", "destination": "D1", "volume": "2"},
    ])
    resp = lineage_quarantine(client, ["A1"])
    path = {w["well"]: w["path"] for w in resp.json()["wells"]}["D1"]
    assert path == ["A1", "B1", "D1"]


def test_cycle_wells_counted_once(client):
    register(client, "A1", "S1", "100")
    batch(client, "b1", [{"source": "A1", "destination": "B1", "volume": "10"}])
    batch(client, "b2", [{"source": "B1", "destination": "C1", "volume": "5"}])
    # C1 -> B1 回到上游形成环
    batch(client, "b3", [{"source": "C1", "destination": "B1", "volume": "1"}])
    resp = lineage_quarantine(client, ["A1"])
    assert resp.status_code == 201, resp.text
    wells = resp.json()["wells"]
    assert [w["well"] for w in wells] == ["A1", "B1", "C1"]
    # B1 的最短路径仍是直接边，不被环路污染
    assert {w["well"]: w["path"] for w in wells}["B1"] == ["A1", "B1"]


def test_multiple_seeds_form_union_with_per_well_paths(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    batch(client, "b1", [{"source": "A1", "destination": "B1", "volume": "5"}])
    batch(client, "b2", [{"source": "A2", "destination": "C1", "volume": "5"}])
    # 小写、重复：排序去重
    resp = lineage_quarantine(client, ["a2", "A1", "a1"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["seed_wells"] == ["A1", "A2"]
    paths = {w["well"]: w["path"] for w in body["wells"]}
    assert sorted(paths) == ["A1", "A2", "B1", "C1"]
    assert paths["B1"] == ["A1", "B1"]
    assert paths["C1"] == ["A2", "C1"]


def test_seed_wells_normalized_and_validated(client):
    register(client, "A1", "S1", "100")
    assert lineage_quarantine(client, []).status_code == 422
    assert lineage_quarantine(client, ["not-a-well"]).status_code == 422
    assert lineage_quarantine(client, ["A1"], reason="").status_code == 422
    assert lineage_quarantine(client, ["A1"], case_id="").status_code == 422


# ------------------------------------------------ 整次拒绝、不留部分状态


def test_unregistered_seed_is_404_and_no_partial_state(client):
    _build_chain(client)
    resp = lineage_quarantine(client, ["A1", "Z9"], request_key="lq-missing")
    assert resp.status_code == 404, resp.text
    # 整次拒绝：闭包内任何孔都不被隔离，申请记录不存在，键未消耗
    for well in ("A1", "B1", "C1", "D1"):
        assert client.get(f"/wells/{well}").json()["quarantine"] is None
    assert client.get("/quarantines").json()["quarantines"] == []
    assert lineage_quarantine(client, ["A1"], request_key="lq-missing").status_code == 201


def test_already_quarantined_descendant_rejects_whole_request(client):
    _build_chain(client)
    assert quarantine(client, ["C1"], case_id="CASE-OTHER", request_key="q-1").status_code == 201

    resp = lineage_quarantine(client, ["A1"], case_id="CASE-1", request_key="lq-1")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "already_quarantined"
    assert detail["conflicting_wells"] == [{"well": "C1", "case_id": "CASE-OTHER"}]
    # 其余孔不被隔离、无谱系申请记录
    assert client.get("/wells/A1").json()["quarantine"] is None
    assert client.get("/wells/B1").json()["quarantine"] is None
    assert client.get("/wells/D1").json()["quarantine"] is None
    assert len(client.get("/quarantines").json()["quarantines"]) == 1
    assert db_count("SELECT COUNT(*) FROM quarantine_lineage_seeds") == 0


def test_active_reservation_on_descendant_rejects_atomically(client):
    _build_chain(client)
    assert client.post("/reservations", json={
        "request_key": "rv-1", "items": [{"well": "C1", "volume": "2"}]
    }).status_code == 201

    resp = lineage_quarantine(client, ["A1"], request_key="lq-blocked")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservations_active"
    assert [b["well"] for b in detail["blocking_wells"]] == ["C1"]
    # 不留部分状态、不消耗键
    assert client.get("/wells/A1").json()["quarantine"] is None
    assert db_count("SELECT COUNT(*) FROM quarantine_requests") == 0
    assert client.post("/reservations/1/cancellations",
                       json={"request_key": "cancel-1"}).status_code == 201
    assert lineage_quarantine(client, ["A1"], request_key="lq-blocked").status_code == 201


# ------------------------------------------------ 幂等与键冲突


def test_lineage_replay_same_key_seed_and_case(client):
    _build_chain(client)
    first = lineage_quarantine(client, ["A1"], request_key="lq-idem")
    assert first.status_code == 201
    first_id = first.json()["quarantine_id"]

    second = lineage_quarantine(client, ["a1"], case_id="CASE-1",
                                reason="原因可以不同", request_key="lq-idem")
    assert second.status_code == 200
    body = second.json()
    assert body["replayed"] is True
    assert body["quarantine_id"] == first_id
    # 重放返回首次算出的孔集与路径
    assert [w["well"] for w in body["wells"]] == ["A1", "B1", "C1", "D1"]
    assert body["wells"][-1]["path"] == ["A1", "B1", "C1", "D1"]
    assert len(client.get("/quarantines").json()["quarantines"]) == 1


def test_replay_returns_first_closure_even_when_lineage_grows(client):
    register(client, "A1", "S1", "100")
    batch(client, "b1", [{"source": "A1", "destination": "B1", "volume": "5"}])
    first = lineage_quarantine(client, ["A1"], request_key="lq-grow")
    assert first.status_code == 201
    # 隔离中的孔不可再移液：先解除，再让谱系图增长
    assert release(client, ["A1", "B1"], request_key="rel-grow").status_code == 201
    batch(client, "b2", [{"source": "B1", "destination": "C1", "volume": "2"}])
    # 闭包不随谱系增长重新扩展：重放仍是首次结果（A1、B1，不含 C1）
    replay = lineage_quarantine(client, ["A1"], request_key="lq-grow")
    assert replay.status_code == 200
    assert [w["well"] for w in replay.json()["wells"]] == ["A1", "B1"]


def test_same_key_other_seed_or_case_conflicts(client):
    _build_chain(client)
    assert lineage_quarantine(client, ["A1"], request_key="lq-key").status_code == 201

    # 同键换种子
    resp = lineage_quarantine(client, ["A2"], request_key="lq-key")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_seed_wells"] == ["A1"]

    # 同键换案件
    resp = lineage_quarantine(client, ["A1"], case_id="CASE-2", request_key="lq-key")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "request_key_conflict"
    # A2 谱系不受影响
    assert client.get("/wells/E1").json()["quarantine"] is None


def test_same_key_cross_kind_conflicts_both_directions(client):
    register(client, "A1", "S1", "100")
    # 先用谱系键
    assert lineage_quarantine(client, ["A1"], request_key="shared-key").status_code == 201
    resp = quarantine(client, ["A1"], request_key="shared-key")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_kind"] == "lineage"

    # 反向：先普通键，再谱系
    register(client, "A2", "S2", "100")
    assert quarantine(client, ["A2"], request_key="shared-key-2").status_code == 201
    resp = lineage_quarantine(client, ["A2"], request_key="shared-key-2")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_kind"] == "explicit_wells"


# ------------------------------------------------ 后续操作沿用隔离限制


def test_downstream_operations_blocked_for_all_closure_wells(client):
    _build_chain(client)
    assert lineage_quarantine(client, ["A1"]).status_code == 201

    # 移液：来源或目标触及闭包任一孔
    for op in (
        {"source": "C1", "destination": "X1", "volume": "1"},
        {"source": "A2", "destination": "D1", "volume": "1"},
    ):
        resp = client.post("/batches", json={"request_key": f"k-{op['destination']}",
                                             "operations": [op]})
        assert resp.status_code == 409
        assert resp.json()["detail"]["reason"] == "well_quarantined"
    # 补液
    resp = client.post("/wells", json={"well": "B1", "sample": "S1", "volume": "1"})
    assert resp.status_code == 409
    # 容量变更
    assert client.put("/wells/C1/capacity", json={"max_capacity": "999"}).status_code == 409
    # 预留
    resp = client.post("/reservations", json={
        "request_key": "rv-x", "items": [{"well": "D1", "volume": "1"}]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    # 计划创建
    resp = client.post("/plans", json={
        "request_key": "plan-x",
        "operations": [{"source": "A2", "destination": "B1", "volume": "1"}]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    # 液体状态与批次记录不变（链上批次 b1 已把 A1 移出 10）
    assert client.get("/wells/A1").json()["volume"] == "90"
    assert client.get("/wells/X1").status_code == 404
    assert db_count("SELECT COUNT(*) FROM batches") == 3


def test_reversal_of_batch_touching_closure_well_blocked(client):
    _build_chain(client)
    assert lineage_quarantine(client, ["A1"], request_key="lq-rev").status_code == 201
    resp = client.post("/batches/2/reversals", json={"request_key": "rev-1"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    assert db_count("SELECT COUNT(*) FROM reversals") == 0

    # 解除全部闭包孔后同键撤销成功。撤销末端批次 b3（触及 C1、D1，无后续
    # 批次阻塞）；它在隔离中本应被 well_quarantined 拦截。
    assert release(client, ["A1", "B1", "C1", "D1"],
                   request_key="rel-1").status_code == 201
    assert client.post("/batches/3/reversals",
                       json={"request_key": "rev-1"}).status_code == 201


def test_plan_confirmation_blocked_after_lineage_quarantine(client):
    _build_chain(client)
    plan = client.post("/plans", json={
        "request_key": "plan-1",
        "operations": [{"source": "A2", "destination": "C1", "volume": "1"}]})
    assert plan.status_code == 201
    plan_id = plan.json()["plan_id"]
    # 计划之后隔离 C1（A1 谱系含 C1）：确认时快照失效
    assert lineage_quarantine(client, ["A1"], request_key="lq-plan").status_code == 201
    resp = client.post(f"/plans/{plan_id}/confirmations",
                       json={"confirm_key": "confirm-1"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "plan_stale"
    reasons = {c["well"]: c["reasons"]
               for c in resp.json()["detail"]["changed_wells"]}
    assert "quarantined" in reasons["C1"]


# ------------------------------------------------ 解除与撤销批次改变谱系


def test_release_then_wells_usable_and_audit_retained(client):
    _build_chain(client)
    created = lineage_quarantine(client, ["A1"]).json()
    assert release(client, ["A1", "B1", "C1", "D1"]).status_code == 201
    # 孔位恢复可用
    assert client.post("/wells", json={"well": "A1", "sample": "S1", "volume": "10"}
                       ).status_code == 201
    assert client.put("/wells/B1/capacity", json={"max_capacity": "200"}
                      ).status_code == 200
    # 申请记录保留（GET /quarantines 中可见，各孔 released）
    stored = client.get("/quarantines").json()["quarantines"]
    assert [q["quarantine_id"] for q in stored] == [created["quarantine_id"]]
    assert all(w["status"] == "released" for w in stored[0]["wells"])
    # 解除后可以重新按谱系隔离
    assert lineage_quarantine(client, ["A1"], request_key="lq-again").status_code == 201


def test_reversed_batch_edges_excluded_from_closure(client):
    register(client, "A1", "S1", "100")
    bid = batch(client, "b1", [{"source": "A1", "destination": "B1", "volume": "5"}])
    # 撤销该批次（B1 恢复为批前不存在）
    assert client.post(f"/batches/{bid}/reversals",
                       json={"request_key": "rev-1"}).status_code == 201
    resp = lineage_quarantine(client, ["A1"], request_key="lq-after-rev")
    assert resp.status_code == 201, resp.text
    # 已撤销批次的边不传播：B1 不再是后代（且已随撤销被删除）
    assert [w["well"] for w in resp.json()["wells"]] == ["A1"]
