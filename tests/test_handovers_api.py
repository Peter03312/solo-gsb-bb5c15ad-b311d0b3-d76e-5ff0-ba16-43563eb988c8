"""装船完结交接（不可变凭证）端到端测试。"""

import json
import threading

from fastapi.testclient import TestClient

from app.main import app


# 同堆栈两层：C001 底（重、晚卸）、C002 顶（轻、早卸）
STACK_SLOTS = {
    "slots": [
        {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "A02", "stack": "A", "tier": 2, "capacity": 100, "arm": 3.0},
    ],
    "adjacency": [],
    "incompatible_classes": [],
    "min_moment": -100000,
    "max_moment": 100000,
}
STACK_MANIFEST = {
    "containers": [
        {"no": "C001", "weight": 30, "hazmat": None, "discharge_order": 2},
        {"no": "C002", "weight": 10, "hazmat": None, "discharge_order": 1},
    ]
}


def _confirm(client):
    sr = client.post("/api/slot-revisions", json=STACK_SLOTS).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=STACK_MANIFEST) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr})
    assert r.status_code == 200, r.text
    return plan


def _schedule(client, pid, crane_count=1):
    r = client.post(f"/api/plans/{pid}/schedules", json={
        "crane_count": crane_count,
        "entries": [
            {"no": "C001", "duration": 10, "release_minute": 0, "deadline_minute": 100},
            {"no": "C002", "duration": 8, "release_minute": 0, "deadline_minute": 100},
        ]})
    assert r.status_code == 201, r.text
    return r.json()


def _ev(seq, etype, cno, crane, minute):
    return {"seq": seq, "event_type": etype, "container_no": cno,
            "crane_no": crane, "minute": minute}


def _finish_all(client, pid, *, f1=10, f2=18, s2=10):
    """登记 C001[0,f1) 桥1、C002[s2,f2) 桥1 的完整合法实绩。"""
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, f1),
        _ev(3, "start", "C002", 1, s2),
        _ev(4, "finish", "C002", 1, f2),
    ]})
    assert r.status_code == 201, r.text


def _ready_plan(client, **kw):
    plan = _confirm(client)
    pid = plan["plan_id"]
    sch = _schedule(client, pid)
    # 基线：C001[0,10) 桥1；C002[10,18) 桥1
    assert {e["container_no"]: (e["start_minute"], e["finish_minute"])
            for e in sch["entries"]} == {"C001": (0, 10), "C002": (10, 18)}
    _finish_all(client, pid, **kw)
    return pid


def _actuals(client, pid, f1, s2, f2, c2_crane=1):
    """登记自定义起止分钟的完整合法实绩（C001[0,f1) 桥1、C002[s2,f2)）。

    c2_crane=2 时 C002 走桥2，可在 C001 完工前并行开工（同堆栈下层 C001 须先完工，
    故仍要求 s2 >= f1；不同桥时区间可重叠）。
    """
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, f1),
        _ev(3, "start", "C002", c2_crane, s2),
        _ev(4, "finish", "C002", c2_crane, f2),
    ]})
    assert r.status_code == 201, r.text


def _issue(client, pid, handover_no="HO-1", **over):
    body = {
        "handover_no": handover_no,
        "cutoff_minute": 20,
        "expected_actuals_version": 1,
        "valid_seqs": [1, 2, 3, 4],
        "max_finish_delay_minutes": 0,
    }
    body.update(over)
    return client.post(f"/api/plans/{pid}/handovers", json=body)


# ---------- 成功签发与固化内容 ----------


def test_issue_voucher_freezes_full_snapshot(client):
    pid = _ready_plan(client)  # C001 准点；C002 准点完工
    r = _issue(client, pid)
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["voucher_no"] and v["handover_no"] == "HO-1"
    assert v["plan_id"] == pid and v["revision_no"] == 1
    assert v["plan_status"] == "confirmed"
    assert v["cutoff_minute"] == 20
    assert v["expected_actuals_version"] == 1
    assert v["actuals_version"] == 1
    assert v["current_actuals_version"] == 1
    assert v["max_finish_delay_minutes"] == 0
    assert v["observed_max_delay_minutes"] == 0
    assert v["container_count"] == 2
    assert v["valid_seqs"] == [1, 2, 3, 4]
    assert len(v["actuals_fingerprint"]) == 64
    assert v["actuals_match"] is True
    assert v["current_actuals_fingerprint"] == v["actuals_fingerprint"]
    assert "HO-1" in v["summary"] and "截止分钟 20" in v["summary"]

    by_no = {c["container_no"]: c for c in v["containers"]}
    c1 = by_no["C001"]
    assert (c1["actual_start_minute"], c1["actual_finish_minute"],
            c1["actual_crane_no"]) == (0, 10, 1)
    assert (c1["baseline_start_minute"], c1["baseline_finish_minute"],
            c1["baseline_crane_no"]) == (0, 10, 1)
    assert (c1["start_delta_minutes"], c1["finish_delta_minutes"],
            c1["finish_delay_minutes"], c1["crane_changed"]) == (0, 0, 0, False)
    c2 = by_no["C002"]
    assert (c2["actual_start_minute"], c2["actual_finish_minute"]) == (10, 18)
    assert c2["finish_delay_minutes"] == 0
    assert v["created_at"]


def test_finish_delay_ignores_early_finish(client):
    # C002 提前完工（12..17，桥2并行；finish_delta=-1）：延误 0，阈值 0 仍可签发
    plan = _confirm(client)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2)
    _actuals(client, pid, f1=10, s2=12, f2=17, c2_crane=2)
    r = _issue(client, pid)
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["observed_max_delay_minutes"] == 0
    c2 = next(c for c in v["containers"] if c["container_no"] == "C002")
    assert c2["finish_delta_minutes"] == -1
    assert c2["finish_delay_minutes"] == 0
    assert c2["actual_crane_no"] == 2 and c2["crane_changed"] is True


def test_finish_exactly_at_cutoff_accepted(client):
    # 截止后无事件且每箱截止前完工（允许恰在截止分钟完工）；延误 2 在阈值内
    pid = _ready_plan(client, f2=20, s2=12)
    r = _issue(client, pid, cutoff_minute=20, max_finish_delay_minutes=2)
    assert r.status_code == 201, r.text


# ---------- 幂等与冲突 ----------


def test_same_handover_no_same_content_returns_original(client):
    pid = _ready_plan(client)
    r1 = _issue(client, pid)
    assert r1.status_code == 201
    r2 = _issue(client, pid)
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert len(client.get("/api/handovers").json()) == 1


def test_same_handover_no_different_content_conflict(client):
    pid = _ready_plan(client)
    assert _issue(client, pid).status_code == 201
    cases = [
        {"cutoff_minute": 21},
        {"expected_actuals_version": 2},
        {"valid_seqs": [1, 2, 3]},
        {"max_finish_delay_minutes": 5},
    ]
    for ch in cases:
        r = _issue(client, pid, **ch)
        assert r.status_code == 409, (ch, r.text)
        assert "冲突" in r.json()["detail"]
    # 凭证不被冲突请求改动
    v = client.get("/api/handovers/by-no/HO-1").json()
    assert v["cutoff_minute"] == 20 and v["max_finish_delay_minutes"] == 0


def test_same_handover_no_other_plan_conflict(client):
    pid1 = _ready_plan(client)
    assert _issue(client, pid1).status_code == 201

    # 另起一个单箱方案（不同方案修订）
    client.post("/api/slot-revisions", json={
        "slots": [{"code": "B01", "stack": "B", "tier": 1, "capacity": 100,
                   "arm": 2.0}],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000})
    client.post("/api/manifest-revisions", json={"containers": [
        {"no": "C009", "weight": 10, "hazmat": None, "discharge_order": 1}]})
    p2 = client.post("/api/plans/compute",
                     json={"slots_revision": 2, "manifest_revision": 2}).json()
    assert client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 2, "manifest_revision": 2}).status_code == 200
    assert client.post(f"/api/plans/{p2['plan_id']}/schedules", json={
        "crane_count": 1, "entries": [
            {"no": "C009", "duration": 5, "release_minute": 0,
             "deadline_minute": 100}]}).status_code == 201
    assert client.post(f"/api/plans/{p2['plan_id']}/actual-events", json={"events": [
        _ev(1, "start", "C009", 1, 0), _ev(2, "finish", "C009", 1, 5)]
    }).status_code == 201
    r = client.post(f"/api/plans/{p2['plan_id']}/handovers", json={
        "handover_no": "HO-1", "cutoff_minute": 20, "expected_actuals_version": 1,
        "valid_seqs": [1, 2], "max_finish_delay_minutes": 0})
    assert r.status_code == 409 and "不同内容" in r.json()["detail"]


def test_one_voucher_per_plan(client):
    pid = _ready_plan(client)
    assert _issue(client, pid, handover_no="HO-1").status_code == 201
    r = _issue(client, pid, handover_no="HO-2")
    assert r.status_code == 409 and "已完成装船完结交接" in r.json()["detail"]


# ---------- 签发前复核：版本/流水号/方案状态 ----------


def test_expected_version_mismatch_conflicts(client):
    pid = _ready_plan(client)
    r = _issue(client, pid, expected_actuals_version=2)
    assert r.status_code == 409 and "版本" in r.json()["detail"]
    # 未生成凭证
    assert client.get("/api/handovers").json() == []


def test_concurrent_correction_between_check_and_insert_leaves_no_stale_voucher(client):
    """凭证写事务持锁期间更正无法插入：更正只能在其前或后。

    交接线程先启动竞争写锁，更正线程随后竞争。更正在前（先抢到锁）-> 凭证 409、
    无凭证行；交接在前 -> 凭证 201、更正随后 201 且查询标示凭证已不匹配当前实绩。
    两种交错都不留旧快照。重复若干轮提高两种交错的覆盖。
    """
    saw_conflict_order = False
    saw_voucher_first_order = False
    for round_no in range(8):
        # 每轮使用不冲突的槽位编号（历史已确认方案仍占用 A01/A02）
        letter = chr(ord("D") + round_no)
        sr = client.post("/api/slot-revisions", json={
            "slots": [
                {"code": f"{letter}01", "stack": letter, "tier": 1,
                 "capacity": 100, "arm": 2.0},
                {"code": f"{letter}02", "stack": letter, "tier": 2,
                 "capacity": 100, "arm": 3.0},
            ],
            "adjacency": [], "incompatible_classes": [],
            "min_moment": -100000, "max_moment": 100000}).json()["slots_revision"]
        mr = client.post("/api/manifest-revisions", json={"containers": [
            {"no": "C001", "weight": 30, "hazmat": None, "discharge_order": 2},
            {"no": "C002", "weight": 10, "hazmat": None, "discharge_order": 1}
        ]}).json()["manifest_revision"]
        plan = client.post("/api/plans/compute", json={}).json()
        pid = plan["plan_id"]
        assert client.post(f"/api/plans/{pid}/confirm", json={
            "slots_revision": sr, "manifest_revision": mr}).status_code == 200
        _schedule(client, pid)
        _finish_all(client, pid)

        results = {}

        def correction(key, pid=pid, round_no=round_no):
            local = TestClient(app)
            r = local.post(f"/api/plans/{pid}/actual-events/correction", json={
                "seq": 3, "expected_version": 1, "request_id": f"CORR-{round_no}",
                "event": {"seq": 3, "event_type": "start", "container_no": "C002",
                          "crane_no": 1, "minute": 11}})
            results[key] = r.status_code

        def handover(key, pid=pid, round_no=round_no):
            local = TestClient(app)
            results[key] = _issue(local, pid, handover_no=f"HO-{round_no}").status_code

        t_h = threading.Thread(target=handover, args=("hov",))
        t_c = threading.Thread(target=correction, args=("corr",))
        t_h.start()
        t_c.start()
        t_h.join()
        t_c.join()

        assert set(results.values()) <= {201, 409}, results
        assert 201 in results.values(), results
        if results["hov"] == 409:
            # 更正在前：凭证不得签发，库里没有该交接号的旧快照
            saw_conflict_order = True
            listing = client.get("/api/handovers").json()
            assert all(v["handover_no"] != f"HO-{round_no}" for v in listing)
        else:
            assert results["corr"] == 201, results
            saw_voucher_first_order = True
            v = client.get(f"/api/handovers/by-no/HO-{round_no}").json()
            assert v["actuals_version"] == 1
            assert v["current_actuals_version"] == 2
            assert v["actuals_match"] is False
            # 凭证固化的仍是更正前内容（C002 开工 10）
            c2 = next(c for c in v["containers"] if c["container_no"] == "C002")
            assert c2["actual_start_minute"] == 10

    # 两种交错都应出现（8 轮、先启动凭证）；若极端调度未覆盖，至少保证逻辑分支被
    # 顺序用例覆盖（test_version_changes_* 与 test_later_actuals_change_*）。
    assert saw_voucher_first_order


def test_concurrent_supplementary_registration_serializes(client):
    """交接与“补登”并发：两事务经写锁串行化。

    补登在全完工后无法再成为新的有效事件（重放必非法），故在尚未完工状态下
    并发：补登完工事件与交接申请竞争写锁——交接先拿锁则因未完工 422、补登随后
    成功；补登先拿锁则交接看到全部完工并 201。两种交错都不会留下旧快照。
    """
    plan = _confirm(client)
    pid = plan["plan_id"]
    _schedule(client, pid)
    # 只登记 C001 完成、C002 开工@10（未完工）
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 1, 10)]})
    assert r.status_code == 201
    results = {}
    barrier = threading.Barrier(2)

    def register(key):
        local = TestClient(app)
        barrier.wait()
        rr = local.post(f"/api/plans/{pid}/actual-events", json={"events": [
            _ev(4, "finish", "C002", 1, 18)]})
        results[key] = rr.status_code

    def handover(key):
        local = TestClient(app)
        barrier.wait()
        results[key] = _issue(local, pid).status_code

    t1 = threading.Thread(target=register, args=("reg",))
    t2 = threading.Thread(target=handover, args=("hov",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert results["reg"] == 201, results
    # 交接要么看到未完工 422（其先拿锁），要么看到全部完工 201
    assert results["hov"] in (201, 422), results
    vouchers = client.get("/api/handovers").json()
    if results["hov"] == 201:
        # 凭证必须覆盖 seq4（否则就是旧快照）
        assert vouchers[0]["valid_seqs"] == [1, 2, 3, 4]
        g = client.get(f"/api/handovers/{vouchers[0]['voucher_no']}").json()
        assert g["actuals_match"] is True
    else:
        assert vouchers == []


def test_unknown_seq_rejected_422(client):
    pid = _ready_plan(client)
    r = _issue(client, pid, valid_seqs=[1, 2, 3, 4, 99])
    assert r.status_code == 422 and "未在方案" in r.json()["detail"]
    assert client.get("/api/handovers").json() == []


def test_valid_seqs_missing_registered_conflicts(client):
    pid = _ready_plan(client)
    r = _issue(client, pid, valid_seqs=[1, 2, 3])
    assert r.status_code == 409 and "已变化" in r.json()["detail"]
    assert client.get("/api/handovers").json() == []


def test_version_changes_after_correction_then_issue_rejected_until_refreshed(client):
    pid = _ready_plan(client)
    # 先更正（不影响合法性：C002 改为[11,19)，仍在截止前、延误1）
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json={
        "seq": 3, "expected_version": 1, "request_id": "CORR-1",
        "event": {"seq": 3, "event_type": "start", "container_no": "C002",
                  "crane_no": 1, "minute": 11}})
    assert r.status_code == 201
    # 用旧版本申请 -> 409
    stale = _issue(client, pid)
    assert stale.status_code == 409
    # 基于新版本与当前流水号重新申请 -> 201
    ok = _issue(client, pid, expected_actuals_version=2)
    assert ok.status_code == 201, ok.text
    assert ok.json()["actuals_version"] == 2


# ---------- 业务前置：完工/截止/延误 ----------


def test_unfinished_plan_rejected(client):
    plan = _confirm(client)
    pid = plan["plan_id"]
    _schedule(client, pid)
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10)]})
    r = client.post(f"/api/plans/{pid}/handovers", json={
        "handover_no": "HO-1", "cutoff_minute": 20, "expected_actuals_version": 1,
        "valid_seqs": [1, 2], "max_finish_delay_minutes": 0})
    assert r.status_code == 422
    body = r.json()
    assert "尚未全部完工" in body["detail"]
    assert body["unfinished_containers"] == ["C002"]


def test_finish_after_cutoff_rejected(client):
    pid = _ready_plan(client, f2=21, s2=13)
    r = _issue(client, pid, cutoff_minute=20)
    assert r.status_code == 422
    body = r.json()
    assert "截止前完工" in body["detail"]
    assert body["containers_finished_after_cutoff"] == ["C002"]
    assert client.get("/api/handovers").json() == []


def test_event_after_cutoff_rejected(client):
    # 截止分钟早于任何完工：逐箱“截止前完工”先命中（C001 完工@10、C002 完工@18
    # 均晚于截止 9），截止后自然存在有效事件；整次拒绝且不生成凭证。
    pid = _ready_plan(client)
    r = _issue(client, pid, cutoff_minute=9)
    assert r.status_code == 422
    body = r.json()
    assert "截止前完工" in body["detail"]
    assert body["containers_finished_after_cutoff"] == ["C001", "C002"]
    assert client.get("/api/handovers").json() == []


def test_over_limit_returns_containers_and_deviation(client):
    # C002 完工 19，基线 18，延误 1；阈值 0 -> 超限
    pid = _ready_plan(client, f2=19, s2=11)
    r = _issue(client, pid, max_finish_delay_minutes=0)
    assert r.status_code == 422
    body = r.json()
    assert "超过允许阈值" in body["detail"]
    assert body["max_finish_delay_minutes"] == 0
    (over,) = body["over_limit_containers"]
    assert over == {
        "container_no": "C002",
        "baseline_finish_minute": 18,
        "actual_finish_minute": 19,
        "finish_delay_minutes": 1,
        "finish_delta_minutes": 1,
        "allowed_max_delay_minutes": 0,
        "excess_minutes": 1,
    }
    # 未生成凭证
    assert client.get("/api/handovers").json() == []


def test_delay_equal_threshold_accepted(client):
    pid = _ready_plan(client, f2=19, s2=11)  # 延误恰为 1
    r = _issue(client, pid, max_finish_delay_minutes=1)
    assert r.status_code == 201, r.text
    assert r.json()["observed_max_delay_minutes"] == 1


def test_multiple_over_limit_sorted(client):
    # 两箱都延误：C001 完工 12（延误 2），C002 完工 21（延误 3）
    pid = _ready_plan(client, f1=12, s2=13, f2=21)
    r = _issue(client, pid, cutoff_minute=30, max_finish_delay_minutes=1)
    assert r.status_code == 422
    over = [c["container_no"] for c in r.json()["over_limit_containers"]]
    assert over == ["C001", "C002"]


# ---------- 方案/实绩状态与 404/409 ----------


def test_unknown_plan_404(client):
    r = client.post("/api/plans/nope/handovers", json={
        "handover_no": "HO-1", "cutoff_minute": 20, "expected_actuals_version": 1,
        "valid_seqs": [], "max_finish_delay_minutes": 0})
    assert r.status_code == 404


def test_draft_plan_rejected(client):
    _confirm(client)  # 录入资料并确认另一个方案（草稿无需确认，只需有资料可核算）
    plan = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{plan['plan_id']}/handovers", json={
        "handover_no": "HO-1", "cutoff_minute": 20, "expected_actuals_version": 1,
        "valid_seqs": [], "max_finish_delay_minutes": 0})
    assert r.status_code == 409 and "确认" in r.json()["detail"]


def test_no_actuals_404(client):
    plan = _confirm(client)
    _schedule(client, plan["plan_id"])
    r = client.post(f"/api/plans/{plan['plan_id']}/handovers", json={
        "handover_no": "HO-1", "cutoff_minute": 20, "expected_actuals_version": 1,
        "valid_seqs": [], "max_finish_delay_minutes": 0})
    assert r.status_code == 404 and "实绩" in r.json()["detail"]


def test_superseded_plan_rejected_and_history_readable(client):
    pid = _ready_plan(client)
    assert _issue(client, pid).status_code == 201
    vn = client.get("/api/handovers").json()[0]["voucher_no"]

    # 新箱清单修订 -> 修订并确认，p1 变 superseded
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "C001", "weight": 15, "hazmat": None, "discharge_order": 2},
            {"no": "C002", "weight": 10, "hazmat": None, "discharge_order": 1},
        ]})
    p2 = client.post(f"/api/plans/{pid}/amend", json={}).json()
    ok = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2})
    assert ok.status_code == 200

    # 被取代的方案拒绝再次交接（即便换新交接号）
    r = _issue(client, pid, handover_no="HO-2")
    assert r.status_code == 409 and "取代" in r.json()["detail"]

    # 历史凭证仍可读；实绩本身未变（仅方案状态变化），指纹仍匹配，plan_status=superseded
    g = client.get(f"/api/handovers/{vn}").json()
    assert g["actuals_match"] is True
    assert g["plan_status"] == "superseded"
    assert client.get(f"/api/plans/{pid}/handovers").status_code == 200
    assert client.get("/api/handovers/by-no/HO-1").status_code == 200


# ---------- 入参校验（422，不写入） ----------


def test_payload_validation(client):
    pid = _ready_plan(client)
    url = f"/api/plans/{pid}/handovers"

    def post(body):
        return client.post(url, json=body)

    base = {"handover_no": "HO-1", "cutoff_minute": 20,
            "expected_actuals_version": 1, "valid_seqs": [1, 2, 3, 4],
            "max_finish_delay_minutes": 0}
    for field, bad in [
        ("cutoff_minute", -1),
        ("expected_actuals_version", 0),
        ("max_finish_delay_minutes", -1),
    ]:
        body = dict(base); body[field] = bad
        assert post(body).status_code == 422, (field, )

    # 交接号缺失/空白
    body = dict(base); body["handover_no"] = "   "
    assert post(body).status_code == 422
    # 流水号非正
    body = dict(base); body["valid_seqs"] = [0, 1, 2]
    assert post(body).status_code == 422
    # 批内流水号重复
    body = dict(base); body["valid_seqs"] = [1, 1, 2, 3]
    r = post(body)
    assert r.status_code == 422 and "重复" in r.text
    # 缺项
    for field in ("handover_no", "cutoff_minute", "expected_actuals_version",
                  "valid_seqs", "max_finish_delay_minutes"):
        body = dict(base); del body[field]
        assert post(body).status_code == 422, field
    # 阈值为 0 合法（非负）
    assert post(base).status_code in (200, 201)
    # 任何校验失败均不写入（库里至多 HO-1 一份凭证）
    assert len(client.get("/api/handovers").json()) == 1


# ---------- 凭证不可变：后续实绩变化 ----------


def test_later_actuals_change_does_not_modify_voucher(client):
    pid = _ready_plan(client)
    r = _issue(client, pid)
    assert r.status_code == 201
    v = r.json()
    fp = v["actuals_fingerprint"]

    # 凭证签发后更正实绩（C002 开工 10 -> 11；重放仍合法，完工 18）
    corr = client.post(f"/api/plans/{pid}/actual-events/correction", json={
        "seq": 3, "expected_version": 1, "request_id": "CORR-1",
        "event": {"seq": 3, "event_type": "start", "container_no": "C002",
                  "crane_no": 1, "minute": 11}})
    assert corr.status_code == 201

    g = client.get(f"/api/handovers/{v['voucher_no']}").json()
    # 凭证固化内容不变
    assert g["actuals_fingerprint"] == fp
    assert g["actuals_version"] == 1
    c2 = next(c for c in g["containers"] if c["container_no"] == "C002")
    assert c2["actual_start_minute"] == 10
    # 查询标示已不再对应当前实绩
    assert g["current_actuals_version"] == 2
    assert g["current_actuals_fingerprint"] != fp
    assert g["actuals_match"] is False

    # 再补登一条合法更正使版本继续推进，标示仍为不匹配
    corr2 = client.post(f"/api/plans/{pid}/actual-events/correction", json={
        "seq": 4, "expected_version": 2, "request_id": "CORR-2",
        "event": {"seq": 4, "event_type": "finish", "container_no": "C002",
                  "crane_no": 1, "minute": 19}})
    assert corr2.status_code == 201
    g2 = client.get(f"/api/handovers/{v['voucher_no']}").json()
    assert g2["actuals_match"] is False and g2["current_actuals_version"] == 3


def test_original_registration_and_correction_still_work_after_voucher(client):
    pid = _ready_plan(client)
    assert _issue(client, pid).status_code == 201
    # 凭证签发不影响原登记接口的同流水号重试语义
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0)]})
    assert r.status_code == 200
    # 更正接口仍可用
    corr = client.post(f"/api/plans/{pid}/actual-events/correction", json={
        "seq": 2, "expected_version": 1, "request_id": "CORR-1",
        "event": {"seq": 2, "event_type": "finish", "container_no": "C001",
                  "crane_no": 1, "minute": 10}})
    # 空更正被拒（分钟/桥都未变）
    assert corr.status_code == 422


# ---------- 指纹稳定性（规范化） ----------


def test_fingerprint_canonical_and_stable(client):
    from app.handovers import actuals_fingerprint

    pid = _ready_plan(client)
    r = _issue(client, pid)
    fp = r.json()["actuals_fingerprint"]
    # 同样的版本/基线/事件（仅提交顺序不同）指纹一致
    baseline = [
        ("C001", "A", 1, 0, 10, 1),
        ("C002", "A", 2, 10, 18, 1),
    ]
    events_a = [
        (1, "start", "C001", 1, 0), (2, "finish", "C001", 1, 10),
        (3, "start", "C002", 1, 10), (4, "finish", "C002", 1, 18),
    ]
    events_b = list(reversed(events_a))
    fp_a = actuals_fingerprint(version=1, baseline_rows=baseline, event_rows=events_a)
    fp_b = actuals_fingerprint(version=1, baseline_rows=list(reversed(baseline)),
                               event_rows=events_b)
    assert fp_a == fp_b == fp
    # 任一现行字段变化 -> 指纹变化
    changed = [
        (1, "start", "C001", 1, 1), (2, "finish", "C001", 1, 10),
        (3, "start", "C002", 1, 10), (4, "finish", "C002", 1, 18),
    ]
    assert actuals_fingerprint(version=1, baseline_rows=baseline,
                               event_rows=changed) != fp
    assert actuals_fingerprint(version=2, baseline_rows=baseline,
                               event_rows=events_a) != fp
