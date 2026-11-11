"""取消、回补与复检的操作边界。"""

from .test_procurement import prepared, execute


def test_cancel_restocks_exactly_once(env):
    c, _, w = env
    p = prepared(env)
    o = execute(c, w, p).json()
    url = f"/api/w/{w}/orders/{o['id']}/cancel"
    assert c.post(url).status_code == 403
    c.headers["Authorization"] = "Bearer demo-approver"
    assert c.post(url).json()["state"] == "cancelled"
    assert c.post(url).json()["state"] == "cancelled"
    snap = c.get(f"/api/w/{w}/snapshot").json()
    assert (
        next(q for q in snap["quote"] if q["id"] == p["lines"][0]["quote_id"])["stock"]
        == 100
    )
    assert (
        c.post(
            f"/api/w/{w}/orders/{o['id']}/receipts",
            json={
                "quote_id": p["lines"][0]["quote_id"],
                "quantity": 1,
                "accepted": 1,
                "idempotency_key": "receipt-0001",
            },
        ).status_code
        == 409
    )


def test_exception_reinspection_and_no_cancel_after_arrival(env):
    c, _, w = env
    p = prepared(env)
    o = execute(c, w, p).json()
    c.post(
        f"/api/w/{w}/orders/{o['id']}/receipts",
        json={
            "quote_id": p["lines"][0]["quote_id"],
            "quantity": 20,
            "accepted": 19,
            "idempotency_key": "receipt-0001",
        },
    )
    issue = c.get(f"/api/w/{w}/snapshot").json()["exception"][0]
    c.headers["Authorization"] = "Bearer demo-approver"
    assert c.post(f"/api/w/{w}/orders/{o['id']}/cancel").status_code == 409
    assert (
        c.post(
            f"/api/w/{w}/exceptions/{issue['id']}/resolve",
            json={"accepted_after_reinspection": 20, "note": "更换故障电源后复检通过"},
        ).status_code
        == 200
    )
    assert c.get(f"/api/w/{w}/snapshot").json()["order"][0]["state"] == "completed"
