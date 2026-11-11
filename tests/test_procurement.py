"""验收业务不变量与故障边界，避免只检查 HTTP 200。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from procurepilot.api import create_app
from procurepilot.storage import Store, records
from procurepilot import runtime


def requirement(c, w, budget="180000", quantity=20, **changes):
    body = {
        "title": "团队设备",
        "budget": budget,
        "delivery_days": 14,
        "allow_split": True,
        "needs": [
            {
                "category": "computer",
                "quantity": quantity,
                "min_memory_gb": 32,
                "interface": "usb-c",
            }
        ],
    }
    body.update(changes)
    return c.post(f"/api/w/{w}/requirements", json=body)


def prepared(env, quantity=20):
    c, _, w = env
    assert (
        requirement(
            c, w, budget="1000000" if quantity > 20 else "180000", quantity=quantity
        ).status_code
        == 201
    )
    p = c.post(f"/api/w/{w}/plans").json()
    assert p["status"] == "feasible"
    c.headers["Authorization"] = "Bearer demo-approver"
    assert c.post(f"/api/w/{w}/plans/{p['id']}/approve").status_code == 200
    c.headers["Authorization"] = "Bearer demo-buyer"
    return p


def execute(c, w, p, key="request-0001"):
    return c.post(f"/api/w/{w}/plans/{p['id']}/execute", json={"idempotency_key": key})


def test_end_to_end_and_idempotency(env):
    c, _, w = env
    p = prepared(env)
    first = execute(c, w, p)
    assert first.status_code == 200
    assert execute(c, w, p).json()["id"] == first.json()["id"]
    assert execute(c, w, p, "different-key").status_code == 409
    snapshot = c.get(f"/api/w/{w}/snapshot").json()
    assert len(snapshot["order"]) == 1
    selected = next(
        q for q in snapshot["quote"] if q["id"] == p["lines"][0]["quote_id"]
    )
    assert selected["stock"] == 80
    assert len(snapshot["outbox"]) == 1
    assert c.post(f"/api/w/{w}/dispatch").json()["delivered"] == 1
    assert c.post(f"/api/w/{w}/dispatch").json()["delivered"] == 0


def test_approval_cannot_be_bypassed(env):
    c, _, w = env
    requirement(c, w)
    p = c.post(f"/api/w/{w}/plans").json()
    assert execute(c, w, p).status_code == 403
    assert c.post(f"/api/w/{w}/plans/{p['id']}/approve").status_code == 403


def test_requirement_change_invalidates_approval(env):
    c, _, w = env
    p = prepared(env)
    requirement(c, w, budget="170000")
    assert execute(c, w, p).status_code == 409
    assert not c.get(f"/api/w/{w}/snapshot").json()["order"]


@pytest.mark.parametrize(
    "budget,quantity,days,interface",
    [
        ("1", 20, 14, "usb-c"),
        ("180000", 101, 14, "usb-c"),
        ("180000", 20, 1, "usb-c"),
        ("180000", 20, 14, "unknown"),
    ],
)
def test_constraints_fail_closed(env, budget, quantity, days, interface):
    c, _, w = env
    requirement(
        c,
        w,
        budget,
        quantity,
        delivery_days=days,
        needs=[
            {
                "category": "computer",
                "quantity": quantity,
                "min_memory_gb": 32,
                "interface": interface,
            }
        ],
    )
    assert c.post(f"/api/w/{w}/plans").json()["status"] == "infeasible"


def test_expired_quote_blocks_execution(env):
    c, db, w = env
    p = prepared(env)
    with db.transaction(w) as s:
        q = next(q for q in records(s, w, "quote") if q.id == p["lines"][0]["quote_id"])
        q.data = {
            **q.data,
            "expires_at": (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(),
        }
    assert execute(c, w, p).status_code == 409


def test_concurrent_different_plans_cannot_oversell(env):
    c, db, w = env
    p1 = prepared(env, quantity=60)
    p2 = c.post(f"/api/w/{w}/plans").json()
    c.headers["Authorization"] = "Bearer demo-approver"
    c.post(f"/api/w/{w}/plans/{p2['id']}/approve")
    c.headers["Authorization"] = "Bearer demo-buyer"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda p: execute(c, w, p, "execute-" + p["id"]).status_code, [p1, p2]
            )
        )
    assert sorted(results) == [200, 409]
    with db.transaction(w) as s:
        assert len(records(s, w, "order")) == 1
        assert all(q.data["stock"] >= 0 for q in records(s, w, "quote"))


def test_partial_receipt_exception_and_replay(env):
    c, _, w = env
    p = prepared(env)
    o = execute(c, w, p).json()
    url = f"/api/w/{w}/orders/{o['id']}/receipts"
    payload = {
        "quote_id": p["lines"][0]["quote_id"],
        "quantity": 10,
        "accepted": 9,
        "note": "一台破损",
        "idempotency_key": "receipt-0001",
    }
    first = c.post(url, json=payload)
    assert first.status_code == 201
    assert c.post(url, json=payload).json()["id"] == first.json()["id"]
    assert c.post(url, json={**payload, "quantity": 11}).status_code == 409
    assert (
        c.post(
            url, json={**payload, "quantity": 11, "idempotency_key": "receipt-0002"}
        ).status_code
        == 422
    )
    snap = c.get(f"/api/w/{w}/snapshot").json()
    assert snap["order"][0]["state"] == "exception"
    assert len(snap["exception"]) == 1


def test_completed_receipt_and_export(env):
    c, _, w = env
    p = prepared(env)
    o = execute(c, w, p).json()
    assert (
        c.post(
            f"/api/w/{w}/orders/{o['id']}/receipts",
            json={
                "quote_id": p["lines"][0]["quote_id"],
                "quantity": 20,
                "accepted": 20,
                "idempotency_key": "receipt-0001",
            },
        ).status_code
        == 201
    )
    assert c.get(f"/api/w/{w}/snapshot").json()["order"][0]["state"] == "completed"
    r = c.get(f"/api/w/{w}/orders/{o['id']}/export")
    assert r.status_code == 200 and "unit_price_cents" in r.text


def test_restart_recovery_does_not_duplicate_plan(env):
    c, db, w = env
    requirement(c, w)
    r = c.post(f"/api/w/{w}/runs", json={}).json()
    with pytest.raises(InterruptedError):
        runtime.resume(db, w, r["id"], stop_after="plan")
    # 新的 Store 模拟进程重启，不能依赖内存 checkpointer。
    restarted = Store(db.engine.url.render_as_string(hide_password=False))
    result = runtime.resume(restarted, w, r["id"])
    assert result["state"] == "completed" and result["steps"] == 4
    runtime.resume(restarted, w, r["id"])
    with restarted.transaction(w) as s:
        assert len(records(s, w, "plan")) == 1
        assert len(records(s, w, "trace")) == 4


def test_budget_exhaustion_is_not_success(env):
    c, _, w = env
    requirement(c, w)
    r = c.post(f"/api/w/{w}/runs", json={"max_steps": 1}).json()
    result = c.post(f"/api/w/{w}/runs/{r['id']}/resume").json()
    assert result["state"] == "budget_exhausted"
    assert result["steps"] == 1


def test_permissions_and_cross_workspace_resource(env):
    c, _, w = env
    p = prepared(env)
    other = c.post("/api/workspaces", json={"name": "其他工作区"}).json()["id"]
    assert execute(c, other, p).status_code == 404
    c.headers["Authorization"] = "Bearer demo-viewer"
    assert requirement(c, w).status_code == 403
    c.headers["Authorization"] = "Bearer invalid"
    assert c.get("/api/workspaces").status_code == 401


def test_scoped_token_denies_other_workspace(env, monkeypatch):
    _, db, w = env
    monkeypatch.setenv(
        "API_TOKENS_JSON", '{"limited":{"role":"buyer","workspaces":[]}}'
    )
    c = TestClient(create_app(db))
    c.headers["Authorization"] = "Bearer limited"
    assert c.get(f"/api/w/{w}/snapshot").status_code == 403
    assert c.get("/api/workspaces").json() == []


def test_invalid_amount_and_unknown_fields(env):
    c, _, w = env
    assert requirement(c, w, budget="12.345").status_code == 422
    assert requirement(c, w, unknown="ignored?").status_code == 422
