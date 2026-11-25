from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest

from app.db import db, write_db
from app.services.divert import detect_scan


def create_routed_scan(client, dest: str = "110") -> int:
    barcode = f"BC-{uuid4().hex[:12]}"
    response = client.post("/api/扫描", json={"条码": barcode, "目的地码": dest})
    assert response.status_code == 200, response.text
    scan_id = response.json()["id"]
    response = client.post(f"/api/扫描/{scan_id}/路由", json={})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "已路由"
    return scan_id


def open_a01_bag(client) -> int:
    response = client.post("/api/封袋/开袋", json={"口门编号": 1})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def chute_a01_occupancy(client) -> int:
    response = client.get("/api/口门")
    assert response.status_code == 200
    chute = next(item for item in response.json() if item["code"] == "A01")
    return chute["在口件数"]


def test_detect_via_legacy_batch_endpoint_decrements_occupancy_and_lights_bucket(client):
    scan_id = create_routed_scan(client)
    assert chute_a01_occupancy(client) >= 1

    response = client.post("/api/回流批次", json={"扫描编号": scan_id, "备注": "错格"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["扫描"]["status"] == "错分回流"
    assert body["扫描"]["divert_batch_id"] == body["批次"]["id"]
    assert body["批次"]["code"].startswith("回流-")
    assert body["回执"]["原口门在口"] == chute_a01_occupancy(client)
    assert body["回执"]["原口门在口"] == 0 or body["回执"]["原口门编号"] != 1

    overview = client.get("/api/总览").json()
    assert overview["分布"]["错分回流"] >= 1
    assert overview["错分桶"]["亮"] is True
    a01 = next(c for c in overview["口门"] if c["code"] == "A01")
    assert a01["在口件数"] == 0
    assert a01["占用事件余额"] == 0

    scans = client.get("/api/扫描", params={"状态": "错分回流"}).json()
    assert any(s["id"] == scan_id for s in scans)

    audit = client.get("/api/审计", params={"扫描编号": scan_id}).json()
    actions = " ".join(f"{e['kind']}:{e['action']}:{e['note']}" for e in audit)
    assert "错分检出" in actions
    assert "在口扣减" in actions

    replay = client.get("/api/审计/重放", params={"扫描编号": scan_id}).json()
    assert replay["重放一致"] is True
    assert replay["最新检出结局"]["batch_code"] == body["批次"]["code"]


def test_detect_from_bag_preserves_original_bag_but_blocks_silent_recollection(client):
    scan_id = create_routed_scan(client)
    bag_id = open_a01_bag(client)
    added = client.post(f"/api/封袋/{bag_id}/入袋", json={"扫描编号": scan_id})
    assert added.status_code == 200, added.text

    detected = client.post(f"/api/扫描/{scan_id}/错分检出", json={})
    assert detected.status_code == 200, detected.text
    receipt = detected.json()["回执"]
    assert receipt["原袋编号"] == bag_id
    assert receipt["原袋关联"]["state"] == "已作废"

    with db() as conn:
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        assert scan["status"] == "错分回流"
        assert scan["bag_id"] is None
        assert scan["original_bag_id"] == bag_id
        relation_count = conn.execute(
            "SELECT COUNT(*) c FROM bag_relations WHERE scan_id=? AND state='已作废'",
            (scan_id,),
        ).fetchone()["c"]
        active_count = conn.execute(
            "SELECT COUNT(*) c FROM bag_relations WHERE scan_id=? AND state='有效'",
            (scan_id,),
        ).fetchone()["c"]
        assert relation_count == 1
        assert active_count == 0

    bags = client.get("/api/封袋").json()
    bag = next(b for b in bags if b["id"] == bag_id)
    assert bag["件数"] == 0
    assert bag["错分检出作废关联数"] == 1

    blocked = client.post(f"/api/封袋/{bag_id}/入袋", json={"扫描编号": scan_id})
    assert blocked.status_code in (400, 409)
    assert "再裁决" in blocked.json()["detail"]

    audit = client.get("/api/审计").json()
    assert any(e["action"] == "检出作废活动袋关联" and e["ref_id"] == bag_id for e in audit)

    replay = client.get(f"/api/错分检出/{scan_id}/重放").json()
    assert replay["重放一致"] is True
    assert replay["折叠结局"]["original_bag_id"] == bag_id
    assert replay["一致性"]["原袋留痕一致"] is True


def test_audit_scan_filter_does_not_match_numeric_id_prefixes(client):
    seeded = client.get("/api/扫描", params={"状态": "待路由"}).json()
    seed_id = seeded[0]["id"]
    assert seed_id == 1
    assert client.post(f"/api/扫描/{seed_id}/路由", json={}).status_code == 200
    assert client.post("/api/错分检出", json={"扫描编号": seed_id}).status_code == 200

    larger_id = None
    for _ in range(10):
        larger_id = create_routed_scan(client)
    assert larger_id == 11
    assert client.post("/api/错分检出", json={"扫描编号": larger_id}).status_code == 200

    rows = client.get("/api/审计", params={"扫描编号": seed_id}).json()
    assert rows
    assert not any(f"扫描{larger_id}" in (row.get("note") or "") for row in rows)
    assert any(row["action"] == "在口扣减" and row["ref_id"] == seed_id for row in rows)


def test_detected_item_can_readjudicate_and_reach_seal_and_shift_reconciliation(client):
    scan_id = create_routed_scan(client)
    bag_id = open_a01_bag(client)
    assert client.post(f"/api/封袋/{bag_id}/入袋", json={"扫描编号": scan_id}).status_code == 200
    assert client.post("/api/错分检出", json={"扫描编号": scan_id}).status_code == 200

    readjudicated = client.post(f"/api/扫描/{scan_id}/再裁决", json={"目的地码": "110"})
    assert readjudicated.status_code == 200, readjudicated.text
    data = readjudicated.json()
    assert data["status"] == "已路由"
    assert data["可再裁决"] is False
    assert data["divert_batch_id"] is not None
    assert data["总览"]["错分桶数量"] == 0

    # Complete the seeded pending scan so shift sealing is a true end-to-end close.
    seeded = client.get("/api/扫描", params={"状态": "待路由"}).json()
    for row in seeded:
        assert client.post(f"/api/扫描/{row['id']}/路由", json={}).status_code == 200

    for row in client.get("/api/扫描", params={"状态": "已路由"}).json():
        response = client.post(f"/api/封袋/{bag_id}/入袋", json={"扫描编号": row["id"]})
        assert response.status_code == 200, response.text

    sealed = client.post(f"/api/封袋/{bag_id}/封口")
    assert sealed.status_code == 200, sealed.text

    reconcile = client.get("/api/班次/对账").json()
    assert reconcile["对账一致"] is True, reconcile["差异"]
    assert reconcile["错分检出事件数"] >= 1
    assert reconcile["再裁决事件数"] >= 1
    assert reconcile["当前在口件数"] == 0

    shift_id = reconcile["班次"]["id"]
    shift_sealed = client.post(f"/api/班次/{shift_id}/封班")
    assert shift_sealed.status_code == 200, shift_sealed.text


def test_half_transaction_failure_rolls_back_chute_and_relation(client):
    scan_id = create_routed_scan(client)
    bag_id = open_a01_bag(client)
    assert client.post(f"/api/封袋/{bag_id}/入袋", json={"扫描编号": scan_id}).status_code == 200

    with pytest.raises(RuntimeError, match="simulated half failure"):
        with write_db() as conn:
            detect_scan(conn, scan_id=scan_id)
            raise RuntimeError("simulated half failure after partial writes")

    with db() as conn:
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        assert scan["status"] == "已入袋"
        assert scan["bag_id"] == bag_id
        assert scan["divert_batch_id"] is None
        assert conn.execute("SELECT COUNT(*) c FROM divert_batches").fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM domain_events WHERE event_type='错分检出'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM bag_relations WHERE scan_id=? AND state='有效'",
            (scan_id,),
        ).fetchone()["c"] == 1
        occupancy = conn.execute(
            "SELECT COALESCE(SUM(delta),0) d FROM occupancy_events WHERE scan_id=?",
            (scan_id,),
        ).fetchone()["d"]
        # route +1 only; rolled-back detection -1 must not survive.
        assert occupancy == 1


@pytest.mark.parametrize("round_no", range(5))
def test_concurrent_detect_and_bag_add_never_dual_terminal(server_url, client, round_no):
    scan_id = create_routed_scan(client)
    bag_id = open_a01_bag(client)
    barrier = threading.Barrier(2)

    def detect():
        barrier.wait()
        return httpx.post(
            f"{server_url}/api/scans/{scan_id}/divert",
            json={"扫描编号": scan_id},
            timeout=15,
            trust_env=False,
        )

    def add_bag():
        barrier.wait()
        return httpx.post(
            f"{server_url}/api/bags/{bag_id}/add",
            json={"扫描编号": scan_id},
            timeout=15,
            trust_env=False,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        detect_response, bag_response = list(pool.map(lambda fn: fn(), [detect, add_bag]))

    assert detect_response.status_code < 500, detect_response.text
    assert bag_response.status_code < 500, bag_response.text
    assert detect_response.status_code < 300 or bag_response.status_code < 300

    with db() as conn:
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        detect_events = conn.execute(
            "SELECT COUNT(*) c FROM domain_events WHERE aggregate_id=? AND event_type='错分检出'",
            (scan_id,),
        ).fetchone()["c"]
        detect_batches = conn.execute(
            "SELECT COUNT(*) c FROM divert_batches b JOIN scans s ON s.divert_batch_id=b.id WHERE s.id=?",
            (scan_id,),
        ).fetchone()["c"]
        active_relations = conn.execute(
            "SELECT COUNT(*) c FROM bag_relations WHERE scan_id=? AND state='有效'",
            (scan_id,),
        ).fetchone()["c"]
        occupancy = conn.execute(
            "SELECT COALESCE(SUM(delta),0) d FROM occupancy_events WHERE scan_id=?",
            (scan_id,),
        ).fetchone()["d"]

    # Detection is allowed after a fully committed bag-add; bag-add can never
    # commit after detection. Therefore the final residual fate is always detected.
    assert scan["status"] == "错分回流"
    assert scan["bag_id"] is None
    if bag_response.status_code < 300:
        # Bag won the lock first and was then superseded by detection.
        assert scan["original_bag_id"] == bag_id
    else:
        # Detection won before any bag link existed; the losing add must not leave one.
        assert scan["original_bag_id"] is None
    assert detect_events == 1
    assert detect_batches == 1
    assert active_relations == 0
    assert occupancy == 0

    replay = httpx.get(
        f"{server_url}/api/审计/重放",
        params={"扫描编号": scan_id},
        timeout=10,
        trust_env=False,
    ).json()
    assert replay["重放一致"] is True

    # Every successful receipt has a matching committed domain event.
    if detect_response.status_code < 300:
        assert detect_response.json()["回执"]["扫描状态"] == "错分回流"
    if bag_response.status_code < 300:
        assert bag_response.json()["status"] == "已入袋"
        with db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM domain_events WHERE aggregate_id=? AND event_type='入袋'",
                (scan_id,),
            ).fetchone()["c"] == 1
