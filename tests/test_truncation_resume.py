"""手动恢复截断响应不重复已成功角色，并保留失败调用用量。"""

from procurepilot import analysis
from procurepilot.models import ModelResponseError
from .test_models import FakeModel
from .test_procurement import requirement


def test_known_truncation_can_resume(env):
    c, db, w = env
    requirement(c, w)

    class TruncatedOnce(FakeModel):
        requests = 0

        def assess(self, *args):
            self.requests += 1
            if self.requests == 2:
                raise ModelResponseError(
                    "output_truncated",
                    {"prompt_tokens": 100, "completion_tokens": 1000},
                    "length",
                )
            return super().assess(*args)

    model = TruncatedOnce()
    first = analysis.run(db, w, "test", model)
    assert (
        first["state"] == "waiting_response"
        and first["steps"] == 1
        and first["tokens"] == 1250
    )
    result = analysis.run(db, w, "test", model, task_id=first["id"])
    assert (
        result["state"] == "completed" and result["steps"] == 3 and model.requests == 4
    )
    assert result["tokens"] == 1550
