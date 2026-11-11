"""请求超时有界、执行租约覆盖请求时限、精简上下文保留权威原件。"""

import pytest
from datetime import datetime
from procurepilot.models import request_timeout_seconds
from procurepilot import analysis
from procurepilot.storage import records
from .test_procurement import requirement
from .test_models import FakeModel


@pytest.mark.parametrize("value,expected", [("90", 90), ("180", 180), ("10", 10)])
def test_timeout_is_configurable(monkeypatch, value, expected):
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", value)
    assert request_timeout_seconds() == expected


@pytest.mark.parametrize("value", ["0", "nan", "infinity", "-1", "181", "invalid"])
def test_timeout_is_bounded(monkeypatch, value):
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError):
        request_timeout_seconds()


def test_context_and_lease_match_long_request(env, monkeypatch):
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "180")
    c, db, w = env
    requirement(c, w)
    c.post(f"/api/w/{w}/plans")

    class Inspect(FakeModel):
        def assess(self, role, context, max_output):
            assert "quote_snapshots" not in context["plan"]
            with db.transaction(w) as s:
                call = records(s, w, "model_call")[-1]
                assert (
                    datetime.fromisoformat(call.data["lease_until"])
                    - datetime.fromisoformat(call.created_at)
                ).total_seconds() > 200
                assert (
                    "quote_snapshots"
                    in records(s, w, "analysis")[-1].data["context"]["plan"]
                )
            return super().assess(role, context, max_output)

    assert analysis.run(db, w, "test", Inspect())["state"] == "completed"
