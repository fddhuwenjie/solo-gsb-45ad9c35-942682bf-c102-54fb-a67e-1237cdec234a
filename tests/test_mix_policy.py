"""样本混合禁配策略端到端测试。"""


def register(client, well, sample, volume, expect=201):
    resp = client.post("/wells", json={"well": well, "sample": sample, "volume": volume})
    assert resp.status_code == expect, resp.text
    return resp if expect != 201 else resp.json()


def change_policy(client, request_key, action, pairs, reason="r", expect=None):
    payload = {
        "request_key": request_key,
        "action": action,
        "pairs": [{"samples": list(pair)} for pair in pairs],
        "reason": reason,
    }
    resp = client.post("/mix-policy/revisions", json=payload)
    if expect is not None:
        assert resp.status_code == expect, resp.text
    return resp


def get_policy(client):
    return client.get("/mix-policy").json()


def active_pairs(client):
    return {tuple(rule["samples"]) for rule in get_policy(client)["rules"]}


# ---------------------------------------------------------------- 查询与修订


def test_empty_policy_is_revision_zero(client):
    body = get_policy(client)
    assert body["revision"] == 0
    assert body["rules"] == []
    assert body["replayed"] is False
    assert client.get("/mix-policy/revisions").json() == {"revisions": []}


def test_add_rules_creates_revision_and_active_rules(client):
    resp = change_policy(
        client, "p1", "add", [("S2", "S1"), ("S3", "S1")], "不相容", expect=201
    )
    body = resp.json()
    assert body["revision"] == 1
    assert body["replayed"] is False
    assert active_pairs(client) == {("S1", "S2"), ("S1", "S3")}
    applied = body["applied"]
    assert applied["revision_id"] == 1
    assert applied["action"] == "add"
    assert applied["reason"] == "不相容"
    assert applied["pairs"] == [
        {"samples": ["S1", "S2"]},
        {"samples": ["S1", "S3"]},
    ]
    # 规则携带其启用修订号
    rules = {tuple(r["samples"]): r["added_revision"] for r in body["rules"]}
    assert rules == {("S1", "S2"): 1, ("S1", "S3"): 1}


def test_revisions_are_monotonic_add_and_remove(client):
    change_policy(client, "p1", "add", [("A", "B")], expect=201)
    change_policy(client, "p2", "add", [("C", "D")], expect=201)
    assert get_policy(client)["revision"] == 2
    resp = change_policy(client, "p3", "remove", [("A", "B")], expect=201)
    assert resp.json()["revision"] == 3
    assert active_pairs(client) == {("C", "D")}
    revisions = client.get("/mix-policy/revisions").json()["revisions"]
    assert [r["revision_id"] for r in revisions] == [1, 2, 3]
    assert [r["action"] for r in revisions] == ["add", "add", "remove"]


def test_add_existing_rule_is_409_without_revision_or_key_consumed(client):
    change_policy(client, "p1", "add", [("A", "B")], expect=201)
    resp = change_policy(client, "p2", "add", [("A", "B"), ("C", "D")], expect=409)
    detail = resp.json()["detail"]
    assert detail["reason"] == "mix_rule_exists"
    assert detail["pairs"] == [{"samples": ["A", "B"]}]
    # 策略不变：C/D 未启用，修订号未推进
    assert get_policy(client)["revision"] == 1
    assert active_pairs(client) == {("A", "B")}
    # p2 未消耗：修正为 C/D 后可直接用同键提交
    change_policy(client, "p2", "add", [("C", "D")], expect=201)


def test_remove_missing_rule_is_409_without_revision_or_key_consumed(client):
    change_policy(client, "p1", "add", [("A", "B")], expect=201)
    resp = change_policy(client, "p2", "remove", [("A", "B"), ("C", "D")], expect=409)
    detail = resp.json()["detail"]
    assert detail["reason"] == "mix_rule_not_found"
    assert detail["pairs"] == [{"samples": ["C", "D"]}]
    assert get_policy(client)["revision"] == 1
    # 整次拒绝：A/B 仍然生效
    assert active_pairs(client) == {("A", "B")}
    # p2 未消耗：改为只撤销存在的 A/B 即可
    change_policy(client, "p2", "remove", [("A", "B")], expect=201)
    assert active_pairs(client) == set()


def test_pair_is_unordered_and_duplicates_dedup(client):
    # 顺序互换视为同一对；请求内重复对（含互换）静默去重
    resp = change_policy(
        client,
        "p1",
        "add",
        [("B", "A"), ("A", "B"), ("A", "B")],
        expect=201,
    )
    assert resp.json()["applied"]["pairs"] == [{"samples": ["A", "B"]}]
    assert active_pairs(client) == {("A", "B")}


def test_replay_same_key_same_target_returns_first_result(client):
    first = change_policy(client, "k1", "add", [("A", "B")], reason="first", expect=201)
    assert first.json()["revision"] == 1
    change_policy(client, "k2", "add", [("C", "D")], expect=201)
    # 同键同目标重放（即使原因不同）-> 200 replayed=true，不产生新修订
    replay = change_policy(client, "k1", "add", [("B", "A")], reason="other", expect=200)
    body = replay.json()
    assert body["replayed"] is True
    assert body["applied"]["revision_id"] == 1
    assert body["applied"]["reason"] == "first"
    assert get_policy(client)["revision"] == 2


def test_same_key_different_target_is_409(client):
    change_policy(client, "k1", "add", [("A", "B")], expect=201)
    # 换样本对
    resp = change_policy(client, "k1", "add", [("A", "C")], expect=409)
    assert resp.json()["detail"]["reason"] == "request_key_conflict"
    # 换动作
    resp = change_policy(client, "k1", "remove", [("A", "B")], expect=409)
    assert resp.json()["detail"]["reason"] == "request_key_conflict"
    # 键未被第二次提交消耗：原目标重放仍然成立
    assert change_policy(client, "k1", "add", [("A", "B")], expect=200).json()[
        "replayed"
    ]


def test_payload_validation(client):
    def post(payload):
        return client.post("/mix-policy/revisions", json=payload)

    base = {"request_key": "v", "action": "add", "reason": "r"}
    assert post({**base, "pairs": []}).status_code == 422
    assert post({**base, "pairs": [{"samples": ["A"]}]}).status_code == 422
    assert post({**base, "pairs": [{"samples": ["A", "A"]}]}).status_code == 422
    assert post({**base, "action": "toggle", "pairs": [{"samples": ["A", "B"]}]}).status_code == 422
    assert post({**base, "request_key": " ", "pairs": [{"samples": ["A", "B"]}]}).status_code == 422
    assert post({**base, "reason": " ", "pairs": [{"samples": ["A", "B"]}]}).status_code == 422
    # 65 对超限
    too_many = [{"samples": [f"S{i}", f"T{i}"]} for i in range(65)]
    assert post({**base, "pairs": too_many}).status_code == 422
    # 1～64 对边界：64 对可过校验（启用扫描决定成败）
    sixty_four = [{"samples": [f"S{i}", f"T{i}"]} for i in range(64)]
    assert post({**base, "pairs": sixty_four}).status_code == 201


# ---------------------------------------------------------------- 启用时存量冲突


def test_enable_blocked_by_existing_well_composition(client):
    register(client, "A1", "S1", "50")
    register(client, "A2", "S2", "30")
    register(client, "B1", "S9", "10")
    # 纯样本孔上启用 S1/S2 -> 允许
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    change_policy(client, "p2", "remove", [("S1", "S2")], expect=201)
    # 制造一个 S1+S2 混合孔
    client.post(
        "/batches",
        json={
            "request_key": "b1",
            "operations": [
                {"source": "A1", "destination": "C1", "volume": "10"},
                {"source": "A2", "destination": "C1", "volume": "10"},
            ],
        },
    )
    # 再启用：C1 已同时含 S1/S2 -> 409，逐孔列出，策略不变
    resp = change_policy(client, "p3", "add", [("S1", "S2")], expect=409)
    detail = resp.json()["detail"]
    assert detail["reason"] == "mix_policy_composition_conflict"
    assert detail["violating_wells"] == [
        {"well": "C1", "pair": {"samples": ["S1", "S2"]}, "samples": ["S1", "S2"]}
    ]
    assert get_policy(client)["revision"] == 2  # 停留在 remove 后的修订号
    assert active_pairs(client) == set()
    # 键未消耗：排除冲突（先解除混合）后同键可重试——这里直接撤销批次清空 C1
    client.post(f"/batches/1/reversals", json={"request_key": "u1"})
    change_policy(client, "p3", "add", [("S1", "S2")], expect=201)

def test_enable_multi_pair_reports_all_conflicting_wells(client):
    register(client, "A1", "S1", "10")
    register(client, "A2", "S2", "10")
    register(client, "B1", "S3", "10")
    register(client, "B2", "S4", "10")
    client.post(
        "/batches",
        json={
            "request_key": "b1",
            "operations": [
                {"source": "A1", "destination": "C1", "volume": "5"},
                {"source": "A2", "destination": "C1", "volume": "5"},
                {"source": "B1", "destination": "D1", "volume": "5"},
                {"source": "B2", "destination": "D1", "volume": "5"},
            ],
        },
    )
    resp = change_policy(
        client, "p1", "add", [("S1", "S2"), ("S3", "S4")], expect=409
    )
    wells = {v["well"]: v["pair"]["samples"] for v in resp.json()["detail"]["violating_wells"]}
    assert wells == {"C1": ["S1", "S2"], "D1": ["S3", "S4"]}


# ---------------------------------------------------------------- 补液拦截


def test_topup_blocked_when_mixing_forbidden_pair(client):
    register(client, "A1", "S1", "100")
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    resp = register(client, "A1", "S2", "10", expect=409)
    detail = resp.json()["detail"]
    assert detail["reason"] == "mix_policy_violation"
    assert detail["well"] == "A1"
    assert detail["pair"] == {"samples": ["S1", "S2"]}
    assert detail["samples"] == ["S1", "S2"]
    # 液体不变
    body = client.get("/wells/A1").json()
    assert body["volume"] == "100"
    assert [c["sample"] for c in body["composition"]] == ["S1"]
    # 规则撤销后补液成功
    change_policy(client, "p2", "remove", [("S1", "S2")], expect=201)
    body = register(client, "A1", "S2", "10", expect=201)
    assert body["volume"] == "110"


def test_topup_same_sample_not_blocked(client):
    register(client, "A1", "S1", "100")
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    # 加入同种样本不产生禁配
    body = register(client, "A1", "S1", "5", expect=201)
    assert body["volume"] == "105"


# ---------------------------------------------------------------- 批次拦截


def _mix(client, request_key, ops, reservation_id=None):
    payload = {"request_key": request_key, "operations": ops}
    if reservation_id is not None:
        payload["reservation_id"] = reservation_id
    return client.post("/batches", json=payload)


def test_batch_step_blocked_and_fully_rolled_back(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    resp = _mix(
        client,
        "b1",
        [
            {"source": "A1", "destination": "B1", "volume": "10"},
            {"source": "A2", "destination": "B1", "volume": "10"},
        ],
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "mix_policy_violation"
    assert detail["well"] == "B1"
    assert detail["pair"] == {"samples": ["S1", "S2"]}
    assert detail["failed_step"] == 2
    assert detail["samples"] == ["S1", "S2"]
    # 整批回滚：第一步的移出/移入也撤销
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/A2").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    # 失败批次落库并消耗 request_key
    failed = client.get(f"/batches/{detail['batch_id']}").json()
    assert failed["status"] == "failed"
    assert failed["operations"] == []
    replay = _mix(
        client,
        "b1",
        [
            {"source": "A1", "destination": "B1", "volume": "10"},
            {"source": "A2", "destination": "B1", "volume": "10"},
        ],
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "failed"
    assert replay.json()["replayed"] is True


def test_batch_checks_full_composition_at_each_step(client):
    # B1 先含 S2（规则未启用时移入），启用后向 B1 移入任何含 S1 的液体即被拦截
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    _mix(client, "b0", [{"source": "A2", "destination": "B1", "volume": "10"}])
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    resp = _mix(client, "b1", [{"source": "A1", "destination": "B1", "volume": "10"}])
    assert resp.status_code == 409
    assert resp.json()["detail"]["failed_step"] == 1
    assert resp.json()["detail"]["samples"] == ["S1", "S2"]


def test_batch_first_step_violation_leaves_no_failed_partial_state(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    register(client, "B1", "S2", "5")
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    resp = _mix(
        client,
        "b1",
        [
            {"source": "A1", "destination": "B1", "volume": "10"},  # 立即冲突
            {"source": "A2", "destination": "C1", "volume": "10"},
        ],
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["failed_step"] == 1
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").json()["volume"] == "5"
    assert client.get("/wells/C1").status_code == 404


def test_drain_then_receive_does_not_falsely_violate(client):
    # 先把来源孔 S1 完全抽干（组成应为空），再向同一孔移入 S2：不含禁配对，
    # 防止实现里残留 0 量样本键导致误判（批次实际执行与计划预演均验证）。
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    resp = _mix(
        client,
        "b1",
        [
            {"source": "A1", "destination": "B1", "volume": "100"},
            {"source": "A2", "destination": "A1", "volume": "10"},
        ],
    )
    assert resp.status_code == 201, resp.text
    assert client.get("/wells/A1").json()["composition"] == [
        {"sample": "S2", "amount": "10", "fraction": "1"}
    ]
    # 同样的顺序在计划预演中也不得误报：新孔 C1 先接收纯 S2，再接收纯 S1，
    # 第 2 步形成禁配必须如实报出（而非因为残留键在别处误报）。
    register(client, "D1", "S2", "100")
    resp = client.post(
        "/plans",
        json={
            "request_key": "pl1",
            "operations": [
                {"source": "D1", "destination": "C1", "volume": "10"},
                {"source": "B1", "destination": "C1", "volume": "10"},
            ],
        },
    )
    # C1 先全 S2，再全 S1 -> 形成禁配，预演必须如实报第 2 步失败
    assert resp.status_code == 409
    assert resp.json()["detail"]["failed_step"] == 2


def test_moving_from_mixed_well_into_clean_well_allowed(client):
    # 规则只按目标孔移入后的完整组成拦截：禁配规则启用后，不涉及被禁样本对
    # 的移液（纯 S1 孔之间）照常执行
    register(client, "A1", "S1", "100")
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    resp = _mix(client, "b1", [{"source": "A1", "destination": "B1", "volume": "10"}])
    assert resp.status_code == 201
    assert client.get("/wells/B1").json()["composition"] == [
        {"sample": "S1", "amount": "10", "fraction": "1"}
    ]


# ---------------------------------------------------------------- 计划：预演、快照与确认


def test_plan_preview_fails_on_policy_violation_without_persisting(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    resp = client.post(
        "/plans",
        json={
            "request_key": "pl1",
            "operations": [
                {"source": "A1", "destination": "C1", "volume": "10"},
                {"source": "A2", "destination": "C1", "volume": "10"},
            ],
        },
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "mix_policy_violation"
    assert detail["failed_step"] == 2
    assert detail["well"] == "C1"
    assert [step["ok"] for step in detail["steps"]] == [True, False]
    # 不落计划、不消耗键
    assert client.get("/plans").json() == {"plans": []}
    assert client.get("/wells/C1").status_code == 404


def test_plan_snapshot_contains_policy_revision_and_rules(client):
    register(client, "A1", "S1", "100")
    change_policy(client, "p1", "add", [("S1", "S2"), ("S3", "S4")], expect=201)
    resp = client.post(
        "/plans",
        json={
            "request_key": "pl1",
            "operations": [{"source": "A1", "destination": "D1", "volume": "10"}],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["mix_policy_revision"] == 1
    assert body["mix_rules"] == [
        {"samples": ["S1", "S2"]},
        {"samples": ["S3", "S4"]},
    ]


def test_plan_confirmation_409_when_policy_revision_changes(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    change_policy(client, "p1", "add", [("S3", "S4")], expect=201)
    # 计划在修订 1 时创建（操作本身合法）
    resp = client.post(
        "/plans",
        json={
            "request_key": "pl1",
            "operations": [
                {"source": "A1", "destination": "D1", "volume": "10"},
                {"source": "A2", "destination": "E1", "volume": "10"},
            ],
        },
    )
    plan_id = resp.json()["plan_id"]
    # 策略发生任何成功修订（即使与本计划无关）即让确认 409
    change_policy(client, "p2", "add", [("S5", "S6")], expect=201)
    resp = client.post(
        f"/plans/{plan_id}/confirmations", json={"confirm_key": "cf1"}
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "plan_stale"
    assert detail["mix_policy_revision_at_plan"] == 1
    assert detail["mix_policy_revision_now"] == 2
    assert detail["mix_policy_changed"] is True
    # 计划保持待确认、液体未变、confirm_key 未消耗
    assert client.get(f"/plans/{plan_id}").json()["status"] == "awaiting_confirmation"
    assert client.get("/wells/D1").status_code == 404
    assert client.get("/wells/E1").status_code == 404


def test_policy_remove_then_readd_same_rules_still_stales_confirmation(client):
    register(client, "A1", "S1", "100")
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    resp = client.post(
        "/plans",
        json={
            "request_key": "pl1",
            "operations": [{"source": "A1", "destination": "D1", "volume": "10"}],
        },
    )
    plan_id = resp.json()["plan_id"]
    change_policy(client, "p2", "remove", [("S1", "S2")], expect=201)
    change_policy(client, "p3", "add", [("S1", "S2")], reason="again", expect=201)
    # 规则集与计划时完全相同，但修订号已推进 -> 仍然 409
    resp = client.post(
        f"/plans/{plan_id}/confirmations", json={"confirm_key": "cf1"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["mix_policy_revision_at_plan"] == 1
    assert resp.json()["detail"]["mix_policy_revision_now"] == 3


def test_plan_confirms_and_enforces_when_policy_unchanged(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    resp = client.post(
        "/plans",
        json={
            "request_key": "pl1",
            "operations": [
                {"source": "A1", "destination": "D1", "volume": "10"},
                {"source": "A2", "destination": "E1", "volume": "10"},
            ],
        },
    )
    plan_id = resp.json()["plan_id"]
    resp = client.post(
        f"/plans/{plan_id}/confirmations", json={"confirm_key": "cf1"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["replayed"] is False
    assert client.get("/wells/D1").json()["volume"] == "10"
    assert client.get("/wells/E1").json()["volume"] == "10"
    assert client.get(f"/plans/{plan_id}").json()["status"] == "committed"
    # 重放确认不重复扣量
    replay = client.post(
        f"/plans/{plan_id}/confirmations", json={"confirm_key": "cf1"}
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert client.get("/wells/D1").json()["volume"] == "10"


def test_plan_created_before_rule_enabled_confirms_against_current_rules(client):
    # 计划在无禁配时预演通过并固化修订号 0；启用规则后确认 -> plan_stale
    # （不会绕过拦截执行禁配混合）
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    resp = client.post(
        "/plans",
        json={
            "request_key": "pl1",
            "operations": [
                {"source": "A1", "destination": "D1", "volume": "10"},
                {"source": "A2", "destination": "D1", "volume": "10"},
            ],
        },
    )
    plan_id = resp.json()["plan_id"]
    assert resp.json()["mix_policy_revision"] == 0
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    resp = client.post(
        f"/plans/{plan_id}/confirmations", json={"confirm_key": "cf1"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["mix_policy_revision_at_plan"] == 0
    assert resp.json()["detail"]["mix_policy_revision_now"] == 1
    assert client.get("/wells/D1").status_code == 404


# ---------------------------------------------------------------- 撤销拦截


def test_reversal_blocked_when_restored_snapshot_violates_current_policy(client):
    """恢复批前快照会使孔组成命中当前禁配对 -> 409，液体与审计不变。

    自然操作下禁配的两个样本一旦同孔，规则即无法启用（启用扫描拦截），因此
    这里直接构造一条「批前快照含 A+B、当前组成不含」的批次来验证恢复前校验。
    """
    from app import db as db_module
    from app import ledger

    register(client, "B1", "A", "40")
    register(client, "C1", "C", "100")
    resp = _mix(client, "b1", [{"source": "C1", "destination": "B1", "volume": "40"}])
    batch_id = resp.json()["batch_id"]

    # 篡改批前快照：B1 批前除 A 外还含 B（模拟规则变化后恢复会违规的场景）
    conn = db_module.connect()
    try:
        with db_module.write_transaction(conn):
            ledger.ensure_sample(conn, "B")
            b_id = conn.execute(
                "SELECT id FROM samples WHERE name = 'B'"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO batch_snapshot_composition (batch_id, well, sample_id, amount) "
                "VALUES (?, 'B1', ?, '1')",
                (batch_id, b_id),
            )
            # 快照体积同步加大，保证不是容量/预留问题先触发
            conn.execute(
                "UPDATE batch_well_snapshots SET volume = '41' WHERE batch_id = ? AND well = 'B1'",
                (batch_id,),
            )
    finally:
        conn.close()

    # 当前没有任何孔同时含 A/B -> 规则可启用
    change_policy(client, "p1", "add", [("A", "B")], expect=201)
    resp = client.post(
        f"/batches/{batch_id}/reversals", json={"request_key": "u1"}
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "mix_policy_violation"
    assert detail["violating_wells"] == [
        {"well": "B1", "pair": {"samples": ["A", "B"]}, "samples": ["A", "B"]}
    ]
    # 液体与审计不变：批次仍未撤销
    assert client.get(f"/batches/{batch_id}").json()["reversed"] is False
    # 撤销规则移除后，同 request_key 可重试成功
    change_policy(client, "p2", "remove", [("A", "B")], expect=201)
    resp = client.post(
        f"/batches/{batch_id}/reversals", json={"request_key": "u1"}
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------- 与预留/隔离并存


def test_policy_still_enforced_for_reservation_bound_batch(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    # 预留 A1/A2 各 10
    rv = client.post(
        "/reservations",
        json={
            "request_key": "rv1",
            "items": [{"well": "A1", "volume": "10"}, {"well": "A2", "volume": "10"}],
        },
    )
    assert rv.status_code == 201
    reservation_id = rv.json()["reservation_id"]
    # 绑定预留、逐孔兑现，但目标形成 S1+S2 -> 仍被禁配拦截
    resp = _mix(
        client,
        "b1",
        [
            {"source": "A1", "destination": "B1", "volume": "10"},
            {"source": "A2", "destination": "B1", "volume": "10"},
        ],
        reservation_id=reservation_id,
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "mix_policy_violation"
    assert detail["batch_id"]  # 失败批次落库
    # 整批回滚：预留保持 active、液体不变
    assert client.get(f"/reservations/{reservation_id}").json()["status"] == "active"
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404


def test_policy_and_quarantine_coexist(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    # 隔离 A1 后补液先撞隔离；解除后撞禁配
    q = client.post(
        "/quarantines",
        json={
            "request_key": "q1",
            "case_id": "CASE-1",
            "reason": "x",
            "wells": ["A1"],
        },
    )
    assert q.status_code == 201
    resp = register(client, "A1", "S2", "1", expect=409)
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    rel = client.post(
        "/quarantines/releases",
        json={"request_key": "r1", "case_id": "CASE-1", "wells": ["A1"]},
    )
    assert rel.status_code == 201
    resp = register(client, "A1", "S2", "1", expect=409)
    assert resp.json()["detail"]["reason"] == "mix_policy_violation"


# ---------------------------------------------------------------- Decimal 与边界


def test_policy_rules_are_text_based_and_volume_decimal_intact(client):
    # 禁配规则不涉及体积；验证 Decimal 体积在禁配拦截路径上保持精确
    register(client, "A1", "S1", "0.1")
    register(client, "A2", "S2", "0.2")
    change_policy(client, "p1", "add", [("S1", "S2")], expect=201)
    resp = register(client, "A1", "S2", "0.3", expect=409)
    assert resp.json()["detail"]["well"] == "A1"
    body = client.get("/wells/A1").json()
    assert body["volume"] == "0.1"
    # 规则查询返回稳定的字符串对
    rules = get_policy(client)["rules"]
    assert rules == [
        {"samples": ["S1", "S2"], "added_revision": 1}
    ]
