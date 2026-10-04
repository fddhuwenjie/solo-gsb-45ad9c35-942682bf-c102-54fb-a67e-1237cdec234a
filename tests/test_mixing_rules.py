"""样本混合禁配策略的端到端测试。

- POST /mixing-rules：add/remove、1～64 个无序样本对、原因与 request_key；
  同一对只能有一条生效规则；启用前扫描所有已登记孔的完整组成，已有冲突
  则 409 且策略不变；同键同目标重放、换动作/换样本对 409。
- 补液、批次每一步、计划预演/确认与撤销都按目标孔当时的完整组成拦截禁配。
- 计划快照包含策略修订号，规则任何增删都使确认返回 409 plan_stale。
"""

import threading
from decimal import Decimal

from app import db as db_module, ledger


def register(client, well, sample, volume):
    resp = client.post(
        "/wells", json={"well": well, "sample": sample, "volume": volume}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def amounts(body):
    return {c["sample"]: Decimal(c["amount"]) for c in body["composition"]}


def rule_request(client, request_key, action, pairs, reason="r"):
    return client.post(
        "/mixing-rules",
        json={
            "request_key": request_key,
            "action": action,
            "reason": reason,
            "pairs": [{"samples": list(p)} for p in pairs],
        },
    )


def policy(client):
    return client.get("/mixing-rules").json()


def reason(resp):
    return resp.json().get("detail", resp.json()).get("reason")


def batch(client, request_key, operations, reservation_id=None):
    payload = {"request_key": request_key, "operations": operations}
    if reservation_id is not None:
        payload["reservation_id"] = reservation_id
    return client.post("/batches", json=payload)


# ---------------------------------------------------------------- 请求校验与查询


def test_policy_initially_empty_revision_zero(client):
    body = policy(client)
    assert body == {"revision": 0, "rules": []}


def test_add_rule_persists_with_reason_and_bumps_revision_once(client):
    resp = rule_request(client, "m1", "add", [("S1", "S2")], reason="不可混用")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["action"] == "add"
    assert body["revision_after"] == 1
    assert body["rules"][0]["samples"] == ["S1", "S2"]
    assert body["rules"][0]["reason"] == "不可混用"
    assert body["rules"][0]["status"] == "active"
    assert body["policy"]["revision"] == 1
    # 一次请求多对只把修订号 +1（不是按对递增）
    resp = rule_request(client, "m2", "add", [("S3", "S4"), ("S5", "S6")])
    assert resp.status_code == 201, resp.text
    assert resp.json()["revision_after"] == 2
    rules = policy(client)["rules"]
    assert [r["samples"] for r in rules] == [
        ["S1", "S2"],
        ["S3", "S4"],
        ["S5", "S6"],
    ]


def test_pairs_are_unordered_and_deduped(client):
    # 反向书写、重复对与首尾空白差异在同一请求内去重（样本名区分大小写，
    # 与既有 samples 表语义一致，故 s1/s2 是另一对样本）
    resp = rule_request(
        client,
        "m1",
        "add",
        [("S2", "S1"), ("S1", "S2"), (" S1 ", "S2 ")],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["pairs"] == [["S1", "S2"]]
    assert len(policy(client)["rules"]) == 1


def test_same_pair_identical_samples_or_bad_count_is_422(client):
    # 同一对必须是两个不同样本
    resp = rule_request(client, "m-bad", "add", [("S1", "S1")])
    assert resp.status_code == 422
    # 空对 / 单样本 / 三个样本均为参数非法
    for bad_pairs in ([], [[], ["S1"], ["S1", "S2", "S3"]]):
        resp = client.post(
            "/mixing-rules",
            json={
                "request_key": f"m-{id(bad_pairs)}",
                "action": "add",
                "reason": "r",
                "pairs": [{"samples": p} for p in bad_pairs],
            },
        )
        assert resp.status_code == 422, bad_pairs
    # 超过 64 对
    too_many = [("S1", f"X{i}") for i in range(65)]
    resp = rule_request(client, "m-many", "add", too_many)
    assert resp.status_code == 422
    # 恰好 64 对可以（无孔含这些样本，不会组成冲突）
    ok = [("Y1", f"Y{i}") for i in range(2, 65)]
    resp = rule_request(client, "m-64", "add", ok)
    assert resp.status_code == 201, resp.text


def test_unknown_action_is_422(client):
    resp = client.post(
        "/mixing-rules",
        json={
            "request_key": "m1",
            "action": "pause",
            "reason": "r",
            "pairs": [{"samples": ["S1", "S2"]}],
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------- 启用前组成扫描


def test_add_rejected_when_any_registered_well_already_contains_pair(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    # 先制造一个同时含 S1、S2 的孔
    assert batch(
        client,
        "b1",
        [
            {"source": "A1", "destination": "B1", "volume": "10"},
            {"source": "A2", "destination": "B1", "volume": "5"},
        ],
    ).status_code == 201

    resp = rule_request(client, "m-block", "add", [("S1", "S2")])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "mixing_composition_conflict"
    assert detail["conflicting_wells"] == [
        {
            "well": "B1",
            "conflicting_pairs": [
                {"samples": ["S1", "S2"], "amounts": {"S1": "10", "S2": "5"}}
            ],
        }
    ]
    # 策略不变、请求未消耗键
    assert policy(client) == {"revision": 0, "rules": []}
    replay = rule_request(client, "m-block", "add", [("S9", "S8")])
    assert replay.status_code == 201  # 键未消耗，可用于不同请求


def test_add_is_atomic_when_any_pair_conflicts(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    assert batch(
        client, "b1", [{"source": "A2", "destination": "B1", "volume": "5"}]
    ).status_code == 201
    register(client, "A3", "S3", "30")
    # S2/S1 无冲突（没有孔同时含），S2/? ... 构造：B1 含 S2，加入冲突对 (S2,S9)?
    # 直接让第二对命中：补液 S2 与 S3 从未同孔 -> 都通过；再制造一对冲突。
    register(client, "A4", "S4", "10")
    assert batch(
        client,
        "b2",
        [
            {"source": "A1", "destination": "C1", "volume": "10"},
            {"source": "A4", "destination": "C1", "volume": "2"},
        ],
    ).status_code == 201
    resp = rule_request(
        client, "m-multi", "add", [("S2", "S3"), ("S1", "S4")]
    )
    assert resp.status_code == 409
    assert reason(resp) == "mixing_composition_conflict"
    wells = [w["well"] for w in resp.json()["detail"]["conflicting_wells"]]
    assert wells == ["C1"]
    # 整次回滚：不冲突的 S2/S3 也没有生效
    assert policy(client)["rules"] == []
    assert policy(client)["revision"] == 0


def test_add_same_active_pair_returns_409_rule_active(client):
    assert rule_request(client, "m1", "add", [("S1", "S2")]).status_code == 201
    resp = rule_request(client, "m2", "add", [("S2", "S1")])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "mixing_rule_active"
    assert detail["active_pairs"] == [
        {"samples": ["S1", "S2"], "rule_id": 1}
    ]
    # 修订号不再增加
    assert policy(client)["revision"] == 1


# ---------------------------------------------------------------- 幂等


def test_request_key_replay_same_target_200_and_conflict_on_change(client):
    first = rule_request(client, "k1", "add", [("S1", "S2"), ("S3", "S4")], reason="一")
    assert first.status_code == 201
    # 同键同动作同样本对（顺序无关）、不同原因 -> 重放首次结果
    again = rule_request(client, "k1", "add", [("S4", "S3"), ("S2", "S1")], reason="二")
    assert again.status_code == 200
    body = again.json()
    assert body["replayed"] is True
    assert body["reason"] == "一"
    assert body["revision_after"] == 1
    # 同键换样本对 -> 409
    conflict = rule_request(client, "k1", "add", [("S1", "S9")])
    assert conflict.status_code == 409
    assert reason(conflict) == "request_key_conflict"
    assert conflict.json()["detail"]["existing_action"] == "add"
    # 同键换动作 -> 409
    conflict2 = rule_request(client, "k1", "remove", [("S1", "S2"), ("S3", "S4")])
    assert conflict2.status_code == 409
    assert reason(conflict2) == "request_key_conflict"
    # 被拒绝的换目标没有改变策略
    assert policy(client)["revision"] == 1


def test_remove_key_replay_and_cross_target_conflict(client):
    rule_request(client, "add1", "add", [("S1", "S2")])
    rm = rule_request(client, "rm1", "remove", [("S1", "S2")])
    assert rm.status_code == 201
    assert rm.json()["revision_after"] == 2
    assert rm.json()["removed_rules"] == [
        {"rule_id": 1, "samples": ["S1", "S2"], "created_request_key": "add1"}
    ]
    again = rule_request(client, "rm1", "remove", [("S2", "S1")])
    assert again.status_code == 200 and again.json()["replayed"] is True
    other = rule_request(client, "rm1", "remove", [("S8", "S9")])
    assert other.status_code == 409 and reason(other) == "request_key_conflict"


# ---------------------------------------------------------------- remove 语义


def test_remove_deactivates_rule_and_pair_can_be_readded(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    rule_request(client, "m-add", "add", [("S1", "S2")])
    # 规则生效中移液被拦截
    blocked = batch(
        client, "b-block", [{"source": "A1", "destination": "A2", "volume": "1"}]
    )
    assert blocked.status_code == 409 and reason(blocked) == "mixing_conflict"

    rm = rule_request(client, "m-rm", "remove", [("S1", "S2")])
    assert rm.status_code == 201
    assert policy(client)["rules"] == []
    assert policy(client)["revision"] == 2
    # 撤销后移液放行
    ok = batch(client, "b-ok", [{"source": "A1", "destination": "A2", "volume": "1"}])
    assert ok.status_code == 201, ok.text
    # 撤销后该对可重新启用（此时 A2 已同时含 S1/S2 -> 组成冲突）
    re_add = rule_request(client, "m-readd", "add", [("S1", "S2")])
    assert re_add.status_code == 409
    assert reason(re_add) == "mixing_composition_conflict"
    # 先把混合解除（撤销批次）再启用
    bid = ok.json()["batch_id"]
    rr = client.post(
        f"/batches/{bid}/reversals", json={"request_key": "undo"}
    )
    assert rr.status_code == 201, rr.text
    re_add2 = rule_request(client, "m-readd", "add", [("S1", "S2")])
    assert re_add2.status_code == 201
    # 新规则是新的 active 行（审计保留），修订号继续推进
    assert re_add2.json()["rules"][0]["rule_id"] == 2
    assert policy(client)["revision"] == 3


def test_remove_unknown_pair_still_records_request_and_bumps_revision(client):
    resp = rule_request(client, "rm-nope", "remove", [("S1", "S2")])
    assert resp.status_code == 201, resp.text
    assert resp.json()["removed_rules"] == []
    assert policy(client)["revision"] == 1


# ---------------------------------------------------------------- 补液拦截


def test_topup_blocked_by_full_composition_after_mix(client):
    register(client, "A1", "S1", "100")
    rule_request(client, "m1", "add", [("S1", "S2")])
    resp = client.post(
        "/wells", json={"well": "A1", "sample": "S2", "volume": "10"}
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "mixing_conflict"
    assert detail["well"] == "A1"
    assert detail["pair"] == ["S1", "S2"]
    assert "failed_step" not in detail
    # 液体不变
    state = client.get("/wells/A1").json()
    assert state["volume"] == "100"
    assert set(amounts(state)) == {"S1"}
    # 补液键未消耗：补液没有 request_key，直接重试放行同样本补液
    same = register(client, "A1", "S1", "5")
    assert same["volume"] == "105"
    # 未登记样本与现有样本不冲突时也放行（新孔登记从不冲突）
    assert register(client, "A2", "S2", "60")["volume"] == "60"


# ---------------------------------------------------------------- 批次逐步拦截


def test_batch_each_step_checked_against_then_full_composition(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    register(client, "A3", "S3", "100")
    rule_request(client, "m1", "add", [("S2", "S3")])

    # 第 1 步 A1(S1)->B1 合法；第 2 步 A2(S2)->B1 合法；
    # 第 3 步 A3(S3)->B1 后 B1 完整组成 {S1,S2,S3} 命中 (S2,S3) -> 整批回滚
    resp = batch(
        client,
        "b1",
        [
            {"source": "A1", "destination": "B1", "volume": "10"},
            {"source": "A2", "destination": "B1", "volume": "10"},
            {"source": "A3", "destination": "B1", "volume": "5"},
        ],
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "mixing_conflict"
    assert detail["well"] == "B1"
    assert detail["pair"] == ["S2", "S3"]
    assert detail["failed_step"] == 3
    assert "batch_id" in detail
    # 整批回滚：前两步的移液同样撤销
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/A2").json()["volume"] == "100"
    assert client.get("/wells/A3").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    # 失败批次落库并消耗 request_key：重放返回失败记录
    replay = batch(
        client,
        "b1",
        [
            {"source": "A1", "destination": "B1", "volume": "10"},
            {"source": "A2", "destination": "B1", "volume": "10"},
            {"source": "A3", "destination": "B1", "volume": "5"},
        ],
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["status"] == "failed"


def test_batch_move_out_then_in_is_checked_per_step(client):
    # 同一批内先把禁配样本移出、再移入另一个样本：按目标孔每一步当时组成
    register(client, "A1", "S1", "100")
    register(client, "B1", "S2", "100")
    register(client, "C1", "S3", "100")
    # B1 先装入 S1（B1={S1,S2}）—— 需有规则 (S1,S3) 而非 (S1,S2)
    rule_request(client, "m1", "add", [("S1", "S3")])
    # C1(S3)->A1(S1) 第 1 步即冲突
    resp = batch(
        client,
        "b-bad",
        [
            {"source": "A1", "destination": "B1", "volume": "10"},
            {"source": "C1", "destination": "A1", "volume": "1"},
        ],
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["failed_step"] == 2
    assert resp.json()["detail"]["well"] == "A1"
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").json()["volume"] == "100"
    assert client.get("/wells/C1").json()["volume"] == "100"


# ---------------------------------------------------------------- 计划预演与确认


def test_plan_preview_mixing_failure_creates_no_plan(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    rule_request(client, "m1", "add", [("S1", "S2")])
    resp = client.post(
        "/plans",
        json={
            "request_key": "p-bad",
            "operations": [{"source": "A1", "destination": "A2", "volume": "10"}],
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "mixing_conflict"
    assert detail["failed_step"] == 1
    assert detail["well"] == "A2"
    assert detail["pair"] == ["S1", "S2"]
    step = detail["steps"][0]
    assert step["ok"] is False and step["reason"] == "mixing_conflict"
    # 不落计划、不消耗键
    assert client.get("/plans").json()["plans"] == []
    retry = client.post(
        "/plans",
        json={
            "request_key": "p-bad",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "10"}
            ],
        },
    )
    assert retry.status_code == 201  # 键未消耗


def test_plan_snapshot_carries_policy_revision(client):
    register(client, "A1", "S1", "100")
    rule_request(client, "m1", "add", [("S8", "S9")])
    resp = client.post(
        "/plans",
        json={
            "request_key": "p1",
            "operations": [{"source": "A1", "destination": "B1", "volume": "10"}],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["mixing_policy_revision"] == 1
    got = client.get(f"/plans/{body['plan_id']}").json()
    assert got["mixing_policy_revision"] == 1


def test_rule_change_after_plan_makes_confirmation_409_plan_stale(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    plan = client.post(
        "/plans",
        json={
            "request_key": "p1",
            "operations": [{"source": "A2", "destination": "C1", "volume": "5"}],
        },
    )
    assert plan.status_code == 201
    pid = plan.json()["plan_id"]
    assert plan.json()["mixing_policy_revision"] == 0

    # 规则变化即使与本次移液完全无关（S8/S9），修订号变化也使确认失效
    rule_request(client, "m1", "add", [("S8", "S9")])
    resp = client.post(
        f"/plans/{pid}/confirmations", json={"confirm_key": "c1"}
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "plan_stale"
    assert detail["changed_wells"] == []
    assert detail["policy_changed"] is True
    assert detail["mixing_policy_revision_at_plan"] == 0
    assert detail["mixing_policy_revision_now"] == 1
    # 计划保持待确认、液体未动
    assert client.get(f"/plans/{pid}").json()["status"] == "awaiting_confirmation"
    assert client.get("/wells/A2").json()["volume"] == "100"
    assert client.get("/wells/C1").status_code == 404
    # 修订号不会退回：原 confirm_key 重试仍是 plan_stale
    again = client.post(
        f"/plans/{pid}/confirmations", json={"confirm_key": "c1"}
    )
    assert again.status_code == 409 and reason(again) == "plan_stale"


def test_plan_creation_itself_rejects_concurrent_rule_change(client, monkeypatch):
    # 只读快照事务不取写锁：在预演期间提交一条规则，随后的写事务二次核对
    # 读到新修订号 -> 409 plan_stale，不落计划、不消耗 request_key。
    register(client, "A1", "S1", "100")
    gate = _Gate()
    original_simulate = ledger._simulate_transfers

    def gated_simulate(snapshot, operations, held, names, forbidden):
        gate.entered.set()
        assert gate.release.wait(10.0)
        return original_simulate(snapshot, operations, held, names, forbidden)

    monkeypatch.setattr(ledger, "_simulate_transfers", gated_simulate)

    def create():
        return client.post(
            "/plans",
            json={
                "request_key": "p-race",
                "operations": [
                    {"source": "A1", "destination": "B1", "volume": "5"}
                ],
            },
        )

    thread, result = _run_thread(create)
    gate.wait_inside()
    # 只读事务持有的是读快照：规则写入可以正常提交
    rule_resp = rule_request(client, "m-race", "add", [("S8", "S9")])
    assert rule_resp.status_code == 201, rule_resp.text
    gate.release.set()

    resp = _join(thread, result)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "plan_stale"
    assert detail["policy_changed"] is True
    assert detail["mixing_policy_revision_at_plan"] == 0
    assert detail["mixing_policy_revision_now"] == 1
    # 不落计划、不消耗键
    assert client.get("/plans").json()["plans"] == []
    retry = client.post(
        "/plans",
        json={
            "request_key": "p-race",
            "operations": [{"source": "A1", "destination": "B1", "volume": "5"}],
        },
    )
    assert retry.status_code == 201, retry.text
    assert retry.json()["mixing_policy_revision"] == 1


def test_plan_confirms_and_enforces_rules_at_confirmation(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    register(client, "A3", "S3", "100")
    # 当前规则允许 S1/S2 同孔，但禁止 S2/S3：计划把 S1 与 S2 混合合法
    rule_request(client, "m1", "add", [("S2", "S3")])
    plan = client.post(
        "/plans",
        json={
            "request_key": "p1",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "10"},
                {"source": "A2", "destination": "B1", "volume": "5"},
            ],
        },
    )
    assert plan.status_code == 201, plan.text
    pid = plan.json()["plan_id"]
    ok = client.post(f"/plans/{pid}/confirmations", json={"confirm_key": "c1"})
    assert ok.status_code == 201, ok.text
    state = client.get("/wells/B1").json()
    assert amounts(state) == {"S1": Decimal("10"), "S2": Decimal("5")}
    # 确认重放不重复扣量
    replay = client.post(
        f"/plans/{pid}/confirmations", json={"confirm_key": "c1"}
    )
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert client.get("/wells/A1").json()["volume"] == "90"
    assert client.get("/wells/A2").json()["volume"] == "95"


# ---------------------------------------------------------------- 撤销按当前规则核对


def test_reversal_blocked_when_restored_snapshot_violates_current_rule(
    client, monkeypatch
):
    # F1 初始同时含 S1/S2，批次把 F1 完全移入新孔 G1（当前禁配对存在于 G1），
    # 通过带外写入一条生效规则模拟「规则在当前液体状态下无法经 API 新增」的
    # 情形（如迁移/预置策略）：撤销会把 F1 恢复为 {S1,S2}，必须拒绝。
    register(client, "F1", "S1", "10")
    register(client, "F1", "S2", "10")
    bid = batch(
        client, "b-drain", [{"source": "F1", "destination": "G1", "volume": "20"}]
    ).json()["batch_id"]
    assert client.get("/wells/F1").json()["volume"] == "0"
    assert client.get("/wells/G1").json()["volume"] == "20"

    # 直接在库里植入生效规则与修订号（API 因 G1 组成冲突无法启用）
    conn = db_module.connect()
    try:
        with db_module.write_transaction(conn):
            cur = conn.execute(
                "INSERT INTO mixing_rule_requests (request_key, action, reason, "
                "revision_after, created_at) VALUES ('seed', 'add', '预置', 1, 'x')"
            )
            seeded = cur.lastrowid
            conn.execute(
                "INSERT INTO mixing_rules (request_id, sample_a, sample_b, status, "
                "reason, created_at) VALUES (?, 'S1', 'S2', 'active', '预置', 'x')",
                (seeded,),
            )
            conn.execute("UPDATE mixing_policy_state SET revision = 1 WHERE id = 1")
    finally:
        conn.close()

    resp = client.post(
        f"/batches/{bid}/reversals", json={"request_key": "undo-1"}
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "mixing_conflict"
    assert detail["violating_wells"] == [
        {"well": "F1", "pairs": [{"samples": ["S1", "S2"], "rule_id": 1}]}
    ]
    # 液体与审计均不变，撤销键未消耗
    assert client.get("/wells/F1").json()["volume"] == "0"
    assert client.get("/wells/G1").json()["volume"] == "20"
    assert client.get(f"/batches/{bid}").json()["reversed"] is False

    # 规则撤销后同 request_key 可撤销成功，恢复快照
    assert rule_request(client, "rm-seed", "remove", [("S1", "S2")]).status_code == 201
    ok = client.post(
        f"/batches/{bid}/reversals", json={"request_key": "undo-1"}
    )
    assert ok.status_code == 201, ok.text
    restored = client.get("/wells/F1").json()
    assert restored["volume"] == "20"
    assert amounts(restored) == {"S1": Decimal("10"), "S2": Decimal("10")}
    assert client.get("/wells/G1").status_code == 404


def test_reversal_after_rule_added_but_restored_composition_clean_is_allowed(client):
    # 规则启用时全部已登记孔无冲突；批次只移动单一成分样本，撤销恢复的快照
    # 只有单一样本，不违反规则 -> 正常撤销。
    register(client, "A1", "S1", "100")
    rule_request(client, "m1", "add", [("S1", "S2")])
    bid = batch(
        client, "b1", [{"source": "A1", "destination": "B1", "volume": "10"}]
    ).json()["batch_id"]
    resp = client.post(
        f"/batches/{bid}/reversals", json={"request_key": "undo"}
    )
    assert resp.status_code == 201, resp.text
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404


# ---------------------------------------------------------------- 串行化


class _Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait_inside(self, timeout=10.0):
        assert self.entered.wait(timeout)


def _run_thread(target):
    result = {}

    def runner():
        try:
            result["value"] = target()
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    return thread, result


def _join(thread, result, timeout=30.0):
    thread.join(timeout)
    assert not thread.is_alive(), "工作线程超时（可能死锁）"
    if "error" in result:
        raise result["error"]
    return result["value"]


def test_rule_add_and_creating_batch_serialize_batch_first(client, monkeypatch):
    # 批次先持锁提交（制造 S1/S2 混合）-> 规则启用在锁后扫描到组成冲突 409，
    # 不会出现「规则生效却有违规孔」的状态。
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    batch_gate = _Gate()
    original_snapshot = ledger._save_pre_batch_snapshot

    def gated_snapshot(conn, batch_id, wells):
        batch_gate.entered.set()
        assert batch_gate.release.wait(10.0)
        return original_snapshot(conn, batch_id, wells)

    monkeypatch.setattr(ledger, "_save_pre_batch_snapshot", gated_snapshot)

    def submit():
        return batch(
            client,
            "b-race",
            [
                {"source": "A1", "destination": "B1", "volume": "10"},
                {"source": "A2", "destination": "B1", "volume": "5"},
            ],
        )

    thread, result = _run_thread(submit)
    batch_gate.wait_inside()

    # 规则请求在写锁上排队
    rule_thread, rule_result = _run_thread(
        lambda: rule_request(client, "m-race", "add", [("S1", "S2")])
    )
    threading.Event().wait(0.2)
    assert thread.is_alive() and rule_thread.is_alive()
    batch_gate.release.set()

    batch_resp = _join(thread, result)
    rule_resp = _join(rule_thread, rule_result)
    assert batch_resp.status_code == 201
    assert rule_resp.status_code == 409
    assert reason(rule_resp) == "mixing_composition_conflict"
    assert policy(client)["revision"] == 0
    assert {c["sample"] for c in client.get("/wells/B1").json()["composition"]} == {
        "S1",
        "S2",
    }


def test_rule_add_and_creating_batch_serialize_rule_first(client, monkeypatch):
    # 规则先持锁提交 -> 批次在锁后执行并按当前规则在冲突步整批回滚。
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    rule_gate = _Gate()
    original_bump = ledger._bump_policy_revision

    def gated_bump(conn):
        rule_gate.entered.set()
        assert rule_gate.release.wait(10.0)
        return original_bump(conn)

    monkeypatch.setattr(ledger, "_bump_policy_revision", gated_bump)

    rule_thread, rule_result = _run_thread(
        lambda: rule_request(client, "m-race", "add", [("S1", "S2")])
    )
    rule_gate.wait_inside()

    # 批次在写锁上排队（规则尚未提交，预检也在锁后，因此它必然看到新规则）
    batch_thread, batch_result = _run_thread(
        lambda: batch(
            client,
            "b-race",
            [
                {"source": "A1", "destination": "B1", "volume": "10"},
                {"source": "A2", "destination": "B1", "volume": "5"},
            ],
        )
    )
    threading.Event().wait(0.2)
    assert rule_thread.is_alive() and batch_thread.is_alive()
    rule_gate.release.set()

    rule_resp = _join(rule_thread, rule_result)
    batch_resp = _join(batch_thread, batch_result)
    assert rule_resp.status_code == 201
    assert policy(client)["revision"] == 1
    assert batch_resp.status_code == 409
    assert reason(batch_resp) == "mixing_conflict"
    assert batch_resp.json()["detail"]["failed_step"] == 2
    # 批次整批回滚：没有任何混合产生
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/A2").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404


def test_concurrent_adds_of_same_pair_with_different_keys(client, monkeypatch):
    # 两个不同 request_key 并发启用同一对：锁内查重先通过、提交时由 active
    # 部分唯一索引兜底；后提交者得到 409 mixing_rule_active 且逐对给出 rule_id。
    gate = _Gate()
    original_conflicts = ledger._existing_composition_conflicts

    def gated_conflicts(conn, pairs):
        gate.entered.set()
        assert gate.release.wait(10.0)
        return original_conflicts(conn, pairs)

    monkeypatch.setattr(ledger, "_existing_composition_conflicts", gated_conflicts)
    threads, results = [], []

    def submit(key):
        r = rule_request(client, key, "add", [("S1", "S2")])
        results.append(r)

    first = threading.Thread(target=submit, args=("add-a",))
    second = threading.Thread(target=submit, args=("add-b",))
    first.start()
    gate.wait_inside()
    second.start()
    threading.Event().wait(0.2)
    assert first.is_alive() and second.is_alive()
    gate.release.set()
    first.join(5.0)
    second.join(5.0)
    assert not first.is_alive() and not second.is_alive()
    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 409]
    winner = next(r for r in results if r.status_code == 201)
    loser = next(r for r in results if r.status_code == 409)
    assert reason(loser) == "mixing_rule_active"
    assert loser.json()["detail"]["active_pairs"] == [
        {"samples": ["S1", "S2"], "rule_id": winner.json()["rules"][0]["rule_id"]}
    ]
    # 只有一条生效规则、修订号只 +1
    assert policy(client)["revision"] == 1
    assert len(policy(client)["rules"]) == 1
