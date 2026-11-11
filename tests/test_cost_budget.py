"""费用按供应商用量和配置单价累计，预算在发起请求之前预留。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from fastapi import HTTPException
from procurepilot import analysis
from procurepilot.storage import records
from .test_procurement import requirement
from .test_models import FakeModel


def test_all_roles_cost_is_accumulated(env):
    c, db, w = env
    requirement(c, w)
    r = analysis.run(db, w, "test", FakeModel(), pricing={"input": "2", "output": "4"})
    assert Decimal(r["cost"]) == Decimal("0.0012")


def test_cost_budget_reserves_before_remote_request(env):
    c, db, w = env
    requirement(c, w)
    model = FakeModel()
    r = analysis.run(
        db, w, "test", model, pricing={"input": "2", "output": "4"}, max_cost="0.000001"
    )
    assert r["state"] == "budget_exhausted" and model.calls == 0


def test_unknown_price_cannot_enforce_cost_budget(env):
    c, db, w = env
    requirement(c, w)
    with pytest.raises(HTTPException) as exc:
        analysis.run(db, w, "test", FakeModel(), max_cost="1")
    assert exc.value.status_code == 422


def test_restart_does_not_reset_deadline(env):
    c, db, w = env
    requirement(c, w)
    model = FakeModel()
    with pytest.raises(InterruptedError):
        analysis.run(db, w, "test", model, stop_after="采购协调员")
    with db.transaction(w) as s:
        task = records(s, w, "analysis")[-1]
        aid = task.id
        task.data = {
            **task.data,
            "deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }
    assert (
        analysis.run(db, w, "test", model, task_id=aid)["state"] == "budget_exhausted"
    )
    assert model.calls == 1
