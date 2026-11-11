"""诊断调用仅发送固定合成文本；失败不自动重试，也不读取采购数据。"""

from procurepilot.model_check import check
from procurepilot.models import ModelConnectionError, ModelReply, Assessment


def test_probe_is_minimal_single_request():
    class Model:
        calls = 0

        def assess(self, role, context, max_output):
            self.calls += 1
            assert role == "接口自检" and max_output == 512
            assert context["evidence_ids"] == []
            assert set(context) == {"task", "evidence_ids"}
            return ModelReply(
                assessment=Assessment(
                    summary="接口可用",
                    evidence_ids=[],
                    missing_information=[],
                    risks=[],
                ),
                prompt_tokens=100,
                completion_tokens=30,
            )

    model = Model()
    result = check(model)
    assert model.calls == 1 and result["status"] == "ok"
    assert result["usage"]["prompt_tokens"] == 100


def test_probe_error_does_not_retry():
    class Model:
        calls = 0

        def assess(self, *args):
            self.calls += 1
            raise ModelConnectionError("remote_closed")

    model = Model()
    result = check(model)
    assert result["status"] == "connection_failed" and model.calls == 1


def test_readonly_user_cannot_spend_model_calls(env):
    c, _, _ = env
    assert (
        c.post(
            "/api/model-check", headers={"Authorization": "Bearer demo-viewer"}
        ).status_code
        == 403
    )
