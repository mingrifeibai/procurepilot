"""百炼 Flash 显式关闭思考，并保持 JSON 输出和用量核算。"""

import json
import httpx
import pytest
from procurepilot.models import ChatCompletionsModel


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://test-workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    ],
)
def test_qwen_non_thinking_contract(monkeypatch, endpoint):
    for k, v in {
        "MODEL_BASE_URL": endpoint,
        "MODEL_NAME": "qwen3.7-flash",
        "MODEL_API_KEY": "fake-qwen-key",
    }.items():
        monkeypatch.setenv(k, v)

    def handler(request):
        payload = json.loads(request.content)
        assert payload["enable_thinking"] is False
        assert "thinking" not in payload
        assert payload["response_format"] == {"type": "json_object"}
        assert str(request.url) == endpoint + "/chat/completions"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "可行",
                                    "evidence_ids": [],
                                    "missing_information": [],
                                    "risks": [],
                                }
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    result = ChatCompletionsModel(httpx.MockTransport(handler)).assess(
        "test", {"evidence_ids": []}, 2048
    )
    assert result.prompt_tokens == 100 and result.completion_tokens == 40
