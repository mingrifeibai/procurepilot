"""工具缓存实际接入运行链路，并验证身份、工作区与失效范围。"""

from .test_procurement import requirement


def test_second_run_reuses_source_but_quote_change_invalidates(env):
    c, _, w = env
    requirement(c, w)

    def run():
        r = c.post(f"/api/w/{w}/runs", json={}).json()
        return c.post(f"/api/w/{w}/runs/{r['id']}/resume").json()

    assert run()["tool_calls"] == 2
    second = run()
    assert second["tool_calls"] == 1 and second["cache_hits"] == 1
    c.post(
        f"/api/w/{w}/quotes",
        json={
            "product_id": "dev-32",
            "supplier": "B",
            "unit_price": "5000",
            "stock": 10,
            "delivery_days": 7,
            "source": "fixture://changed",
        },
    )
    assert run()["tool_calls"] == 2


def test_replan_cannot_read_other_workspace(env):
    c, _, w = env
    requirement(c, w)
    pid = c.post(f"/api/w/{w}/plans").json()["id"]
    other = c.post("/api/workspaces", json={"name": "其他组"}).json()["id"]
    requirement(c, other)
    assert (
        c.post(f"/api/w/{other}/replan", json={"previous_plan_id": pid}).status_code
        == 404
    )


def test_analysis_cancel_records_audit_and_does_not_change_order(env):
    from procurepilot.storage import add

    c, db, w = env
    with db.transaction(w) as s:
        task = add(
            s,
            w,
            "analysis",
            {
                "state": "running",
                "context": {"private_snapshot": "not-for-console"},
                "assessments": [],
            },
        )
        aid = task.id
    result = c.post(f"/api/w/{w}/analyses/{aid}/cancel").json()
    assert result["state"] == "cancelled" and "context" not in result
    snapshot = c.get(f"/api/w/{w}/snapshot").json()
    assert "context" not in snapshot["analysis"][0]
    assert any(a["action"] == "analysis.cancelled" for a in snapshot["audit"])
    assert not snapshot["order"]
