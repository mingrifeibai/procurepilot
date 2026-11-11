"""连接异常不可当作成功，且不能自动重试或输出底层异常敏感内容。"""

import httpx
import pytest
from procurepilot.models import ChatCompletionsModel, ModelConnectionError


@pytest.mark.parametrize(
    "error,kind",
    [
        (httpx.RemoteProtocolError, "remote_closed"),
        (httpx.ReadTimeout, "timeout"),
        (httpx.ConnectError, "connect_failed"),
    ],
)
def test_connection_diagnostics_without_retry(monkeypatch, error, kind):
    for k, v in {
        "MODEL_BASE_URL": "https://model.test/v1",
        "MODEL_NAME": "test",
        "MODEL_API_KEY": "fake-secret",
    }.items():
        monkeypatch.setenv(k, v)
    calls = []

    def handler(request):
        calls.append(1)
        raise error("fake-secret must not escape")

    with pytest.raises(ModelConnectionError) as caught:
        ChatCompletionsModel(httpx.MockTransport(handler)).assess("test", {}, 1000)
    assert caught.value.diagnostic["connection_issue"] == kind
    assert "fake-secret" not in str(caught.value)
    assert len(calls) == 1
