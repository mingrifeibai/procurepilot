"""已知失败可以显式续时，未知结果和费用上限不能被绕过。"""

from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
from procurepilot.storage import records
from procurepilot.continuation import continue_truncated
from .test_procurement import requirement
from .test_models import FakeModel
from procurepilot import analysis
from procurepilot.models import ModelResponseError


def test_explicit_continuation_keeps_old_usage_and_completed_roles(env):
    c, db, w = env
    requirement(c, w)

    class Once(FakeModel):
        requests = 0

        def assess(self, *args):
            self.requests += 1
            if self.requests == 2:
                raise ModelResponseError(
                    "output_truncated",
                    {"prompt_tokens": 10, "completion_tokens": 1000},
                    "length",
                )
            return super().assess(*args)

    model = Once()
    r = analysis.run(db, w, "test", model)
    with db.transaction(w) as s:
        task = records(s, w, "analysis")[-1]
        task.data = {
            **task.data,
            "deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }
    done = continue_truncated(db, w, r["id"], "test", model=model)
    assert (
        done["state"] == "completed" and done["tokens"] == 1460 and model.requests == 4
    )
    with db.transaction(w) as s:
        assert records(s, w, "continuation")[-1].data["tokens_already_used"] == 1160


def test_unknown_result_cannot_be_continued(env):
    c, db, w = env
    requirement(c, w)

    class Unknown:
        def assess(self, *args):
            raise TimeoutError()

    result = analysis.run(db, w, "test", Unknown())
    with pytest.raises(HTTPException):
        continue_truncated(db, w, result["id"], "test", model=FakeModel())
