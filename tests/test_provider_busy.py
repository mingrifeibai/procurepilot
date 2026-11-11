"""明确的繁忙拒绝允许冷却后手动恢复，未知结果仍禁止自动重发。"""

from datetime import datetime, timedelta, timezone
from procurepilot import analysis
from procurepilot.models import ProviderError
from procurepilot.storage import records
from .test_procurement import requirement
from .test_models import FakeModel


def test_busy_provider_waits_and_resumes_without_losing_completed_roles(env):
    c, db, w = env
    requirement(c, w)

    class BusyOnce(FakeModel):
        requests = 0

        def assess(self, *args):
            self.requests += 1
            if self.requests == 2:
                raise ProviderError(429, "1305")
            return super().assess(*args)

    model = BusyOnce()
    first = analysis.run(db, w, "test", model)
    assert first["state"] == "waiting_provider" and first["steps"] == 1
    assert "访问量过大" in first["error"]["hint"]
    assert (
        analysis.run(db, w, "test", model, task_id=first["id"])["state"]
        == "waiting_provider"
    )
    assert model.requests == 2
    with db.transaction(w) as s:
        task = records(s, w, "analysis")[-1]
        task.data = {
            **task.data,
            "retry_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }
    result = analysis.run(db, w, "test", model, task_id=first["id"])
    assert (
        result["state"] == "completed"
        and result["tokens"] == 450
        and model.requests == 4
    )


def test_insufficient_balance_does_not_become_retryable(env):
    c, db, w = env
    requirement(c, w)

    class Balance:
        def assess(self, *args):
            raise ProviderError(429, "1113")

    result = analysis.run(db, w, "test", Balance())
    assert result["state"] == "tool_unavailable" and "欠费" in result["error"]["hint"]
