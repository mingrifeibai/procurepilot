"""增量重规划、工具复用、动态委派与中断恢复的可观察行为验收。"""

from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
from procurepilot import analysis, governance
from procurepilot.storage import records, add
from .test_procurement import requirement
from .test_models import FakeModel


def bundle(quantity=2):
    return [
        {
            "category": "computer",
            "quantity": quantity,
            "min_memory_gb": 32,
            "interface": "usb-c",
        },
        {"category": "monitor", "quantity": 2, "interface": "usb-c"},
        {"category": "dock", "quantity": 2, "interface": "usb-c"},
    ]


def test_incremental_preserves_unchanged_groups_and_requires_new_approval(env):
    c, _, w = env
    requirement(c, w, needs=bundle())
    old = c.post(f"/api/w/{w}/plans").json()
    c.post(
        f"/api/w/{w}/plans/{old['id']}/approve",
        headers={"Authorization": "Bearer demo-approver"},
    )
    requirement(c, w, needs=bundle(3))
    new = c.post(f"/api/w/{w}/replan", json={"previous_plan_id": old["id"]}).json()
    assert new["status"] == "feasible"
    assert new["replanning"]["retained_need_indices"] == [1, 2]
    assert new["replanning"]["affected_need_indices"] == [0]
    assert not new["replanning"]["full_fallback"]
    assert new["delta_cents"] == 580000
    assert (
        c.post(
            f"/api/w/{w}/plans/{new['id']}/execute",
            json={"idempotency_key": "new-plan-exec"},
        ).status_code
        == 403
    )


def test_incremental_falls_back_when_global_budget_changes(env):
    c, _, w = env
    # 首先只能选择较贵的 64G；约束变更后电脑组重算。
    needs = bundle()
    needs[0]["min_memory_gb"] = 64
    requirement(c, w, needs=needs)
    old = c.post(f"/api/w/{w}/plans").json()
    # 新报价使显示器更便宜，但旧组仍固定；新预算必须解除固定才能满足。
    c.post(
        f"/api/w/{w}/quotes",
        json={
            "product_id": "screen-27",
            "supplier": "新供应商",
            "unit_price": "100",
            "stock": 100,
            "delivery_days": 7,
            "source": "fixture://new",
        },
    )
    requirement(c, w, budget="14000", needs=bundle())
    new = c.post(f"/api/w/{w}/replan", json={"previous_plan_id": old["id"]}).json()
    assert new["status"] == "feasible" and new["replanning"]["full_fallback"]
    assert new["total_cents"] <= 1400000


def test_read_cache_isolated_versioned_and_loop_bounded(env):
    _, db, w = env
    calls = []

    def loader():
        calls.append(1)
        return {"answer": len(calls)}

    def read(actor, run, dependency):
        with db.transaction(w) as s:
            return governance.read(s, w, actor, run, "lookup", {}, dependency, loader)

    assert read("a", "run1", 1)[1] is False
    assert read("a", "run2", 1)[1] is True
    assert read("b", "run3", 1)[1] is False
    assert read("a", "run1", 2)[1] is False
    assert read("a", "loop", 2)[1] is True
    read("a", "loop", 2)
    with pytest.raises(HTTPException, match="422"):
        read("a", "loop", 2)
    assert len(calls) == 3


def test_analysis_resumes_after_committed_role_without_repeating(env):
    c, db, w = env
    requirement(c, w)
    model = FakeModel()
    with pytest.raises(InterruptedError):
        analysis.run(db, w, "tester", model, stop_after="采购协调员")
    with db.transaction(w) as s:
        aid = records(s, w, "analysis")[-1].id
    r = analysis.run(db, w, "tester", model, task_id=aid)
    assert r["state"] == "completed" and model.calls == 3 and r["tokens"] == 450
    assert "context" not in r


def test_complex_request_delegates_five_roles(env):
    c, db, w = env
    requirement(c, w, needs=bundle())
    model = FakeModel()
    r = analysis.run(db, w, "tester", model)
    assert r["state"] == "completed" and len(r["assessments"]) == 5 and model.calls == 5
    assert r["tokens"] == 750


def test_uncertain_inflight_call_is_not_reissued(env):
    c, db, w = env
    requirement(c, w)
    model = FakeModel()
    with pytest.raises(InterruptedError):
        analysis.run(db, w, "tester", model, stop_after="采购协调员")
    with db.transaction(w) as s:
        aid = records(s, w, "analysis")[-1].id
        add(
            s,
            w,
            "model_call",
            {
                "analysis_id": aid,
                "state": "pending",
                "role": "寻源员",
                "lease_until": (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                ).isoformat(),
            },
        )
    r = analysis.run(db, w, "tester", model, task_id=aid)
    assert r["state"] == "tool_unavailable" and r["uncertain_usage"]
    assert model.calls == 1


def test_live_call_lease_prevents_second_executor(env):
    c, db, w = env
    requirement(c, w)
    model = FakeModel()
    with pytest.raises(InterruptedError):
        analysis.run(db, w, "tester", model, stop_after="采购协调员")
    with db.transaction(w) as s:
        aid = records(s, w, "analysis")[-1].id
        add(
            s,
            w,
            "model_call",
            {
                "analysis_id": aid,
                "state": "pending",
                "lease_until": (
                    datetime.now(timezone.utc) + timedelta(seconds=90)
                ).isoformat(),
            },
        )
    with pytest.raises(HTTPException) as exc:
        analysis.run(db, w, "tester", model, task_id=aid)
    assert exc.value.status_code == 409 and model.calls == 1


def test_quote_changes_stop_analysis_resume(env):
    c, db, w = env
    requirement(c, w)
    model = FakeModel()
    with pytest.raises(InterruptedError):
        analysis.run(db, w, "tester", model, stop_after="采购协调员")
    with db.transaction(w) as s:
        aid = records(s, w, "analysis")[-1].id
        quote = records(s, w, "quote")[0]
        quote.data = {**quote.data, "stock": 0}
    assert analysis.run(db, w, "tester", model, task_id=aid)["state"] == "stale"
    assert model.calls == 1
