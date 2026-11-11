"""供应商诊断仅暴露状态和数字错误码，不回显原始正文或密钥。"""

import httpx
import pytest
from procurepilot.models import ChatCompletionsModel, ProviderError
from procurepilot import analysis
from .test_procurement import requirement
from procurepilot.storage import records


@pytest.mark.parametrize("code,expected", [("1210", "1210"), ("echo-secret", None)])
def test_safe_provider_error(monkeypatch, code, expected):
    for k, v in {
        "MODEL_BASE_URL": "https://model.test/v1",
        "MODEL_API_KEY": "test-private-key",
        "MODEL_NAME": "test-model",
    }.items():
        monkeypatch.setenv(k, v)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            400,
            json={
                "error": {
                    "code": code,
                    "message": "test-private-key must never be logged",
                }
            },
        )
    )
    with pytest.raises(ProviderError) as caught:
        ChatCompletionsModel(transport).assess("tester", {"evidence_ids": []}, 1000)
    error = caught.value
    assert error.status_code == 400 and error.provider_code == expected
    assert "test-private-key" not in str(vars(error))


def test_rejected_model_request_is_distinct_from_uncertain_timeout(env):
    c, db, w = env
    requirement(c, w)

    class Rejected:
        def assess(self, *args):
            raise ProviderError(401, "1000")

    result = analysis.run(db, w, "tester", Rejected())
    assert result["state"] == "tool_unavailable" and result["uncertain_usage"] is False
    assert result["error"]["http_status"] == 401
    with db.transaction(w) as s:
        assert records(s, w, "model_call")[-1].data["state"] == "rejected"
