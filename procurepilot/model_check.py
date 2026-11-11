"""单请求模型自检：仅发送固定合成文本，不读取工作区、附件或历史对话。"""

import time
from .models import (
    ChatCompletionsModel,
    ProviderError,
    ModelResponseError,
    ModelConnectionError,
)


def check(model=None):
    started = time.perf_counter()
    try:
        reply = (model or ChatCompletionsModel()).assess(
            "接口自检",
            {
                "task": "这只是合成连通性测试。summary 写接口可用，三个数组均返回空数组。",
                "evidence_ids": [],
            },
            512,
        )
        result = {
            "status": "ok",
            "usage": {
                "prompt_tokens": reply.prompt_tokens,
                "completion_tokens": reply.completion_tokens,
            },
        }
    except ProviderError as exc:
        result = {
            "status": "provider_rejected",
            "http_status": exc.status_code,
            "provider_code": exc.provider_code,
            "hint": exc.hint,
        }
    except ModelResponseError as exc:
        result = {"status": "invalid_response", "usage": exc.usage, **exc.diagnostic}
    except ModelConnectionError as exc:
        result = {"status": "connection_failed", **exc.diagnostic}
    return {
        **result,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "external_requests": 1,
        "synthetic_only": True,
    }
