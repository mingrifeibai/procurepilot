"""响应校验失败也保留用量；不保存或回显模型原文。"""

import json
import httpx
import pytest
from procurepilot.models import ChatCompletionsModel, ModelResponseError
from procurepilot import analysis
from procurepilot.storage import records
from .test_procurement import requirement


@pytest.fixture
def configured(monkeypatch):
    for k, v in {
        "MODEL_BASE_URL": "https://open.bigmodel.cn/api/paas/v4",
        "MODEL_NAME": "glm-4.7-flash",
        "MODEL_API_KEY": "fake-key",
    }.items():
        monkeypatch.setenv(k, v)


def response(content, finish="stop"):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 1000},
        },
    )


def test_glm_short_analysis_disables_thinking(configured):
    def handler(request):
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "disabled"}
        assert "JSON Schema" in body["messages"][0]["content"]
        return response(
            json.dumps(
                {
                    "summary": "可行",
                    "evidence_ids": [],
                    "missing_information": [],
                    "risks": [],
                }
            )
        )

    assert (
        ChatCompletionsModel(httpx.MockTransport(handler))
        .assess("test", {"evidence_ids": []}, 1000)
        .completion_tokens
        == 1000
    )


def test_dynamic_verifier_rejects_wrong_quantity_and_resumes_only_verifier(
    env, configured
):
    client, store, wid = env
    requirement(client, wid, budget="30000", quantity=4)
    prefix = f"/api/w/{wid}"
    client.post(prefix + "/plans").raise_for_status()
    roles = []

    def handler(request):
        messages = json.loads(request.content)["messages"]
        rules = messages[0]["content"]
        context = json.loads(messages[1]["content"])
        payload = {
            "summary": "采购核验",
            "evidence_ids": context["evidence_ids"][:1],
            "missing_information": [],
            "risks": [],
        }
        if "你是企业采购的采购协调员" in rules:
            roles.append("coordinator")
            payload.update(
                delegations=[],
                tool_requests=[{"tool": "search_products", "query": "dev-32"}],
            )
        else:
            roles.append("verifier")
            assert "quantity_checks 必填" in rules
            catalog_id = next(
                row["id"]
                for result in context["tool_results"]
                for row in result["output"]["evidence"]
            )
            payload["evidence_ids"] = [catalog_id]
            payload["quantity_checks"] = context["plan"]["need_coverage"]
            if roles.count("verifier") == 1:
                payload["quantity_checks"][0]["required_quantity"] = 5
                payload["missing_information"] = ["还缺 1 台设备"]
        return response(json.dumps(payload, ensure_ascii=False))

    model = ChatCompletionsModel(httpx.MockTransport(handler))
    stopped = analysis.run(
        store,
        wid,
        "buyer",
        model,
        coordination_mode="dynamic",
        catalog_mode="on_demand",
    )
    assert stopped["state"] == "waiting_response"
    assert stopped["error"]["response_issue"] == "quantity_mismatch"
    assert stopped["tokens"] == 2200
    assert [row["role"] for row in stopped["assessments"]] == ["采购协调员"]
    result = analysis.run(store, wid, "buyer", model, task_id=stopped["id"])
    assert roles == ["coordinator", "verifier", "verifier"]
    assert result["state"] == "completed" and result["tokens"] == 3300
    assert result["tool_results"] == stopped["tool_results"]
    assert result["assessments"][-1]["quantity_checks"] == [
        {"need_index": 0, "required_quantity": 4, "allocated_quantity": 4}
    ]
    with store.transaction(wid) as session:
        assert not records(session, wid, "clarification")
        assert not records(session, wid, "order")


def test_truncation_keeps_usage(configured):
    model = ChatCompletionsModel(
        httpx.MockTransport(lambda _: response("", finish="length"))
    )
    with pytest.raises(ModelResponseError) as exc:
        model.assess("test", {}, 1000)
    assert exc.value.diagnostic["response_issue"] == "output_truncated"
    assert exc.value.usage["completion_tokens"] == 1000


def test_bad_field_is_reported_without_its_content(configured):
    content = json.dumps(
        {
            "summary": "ok",
            "evidence_ids": {"SECRET": "sensitive-value"},
            "missing_information": [],
            "risks": [],
        }
    )
    model = ChatCompletionsModel(httpx.MockTransport(lambda _: response(content)))
    with pytest.raises(ModelResponseError) as exc:
        model.assess("test", {}, 1000)
    assert exc.value.diagnostic["invalid_fields"] == ["evidence_ids"]
    assert "sensitive-value" not in str(vars(exc.value))


def test_dynamic_coordinator_must_explicitly_select_tools(configured):
    payload = {
        "summary": "可行",
        "evidence_ids": [],
        "missing_information": [],
        "risks": [],
        "delegations": [],
    }
    model = ChatCompletionsModel(
        httpx.MockTransport(lambda _: response(json.dumps(payload)))
    )
    context = {"coordination_mode": "dynamic", "evidence_ids": []}
    with pytest.raises(ModelResponseError) as exc:
        model.assess("采购协调员", context, 1000)
    assert exc.value.diagnostic["invalid_fields"] == ["tool_requests"]
    assert exc.value.usage["completion_tokens"] == 1000
    payload["tool_requests"] = []
    assert model.assess("采购协调员", context, 1000).assessment.tool_requests == []


@pytest.mark.parametrize("alias", ["E2", "E9"])
def test_on_demand_aliases_resolve_to_authorized_evidence(configured, alias):
    context = {
        "catalog_mode": "on_demand",
        "evidence_ids": ["canonical-quote-id", "catalog:dev-32"],
        "quotes": [{"id": "canonical-quote-id", "product_id": "dev-32"}],
        "tool_results": [{"output": {"evidence": [{"id": "catalog:dev-32"}]}}],
    }

    def handler(request):
        wire = json.loads(json.loads(request.content)["messages"][-1]["content"])
        assert wire["quotes"][0]["id"] == "E1"
        assert wire["tool_results"][0]["output"]["evidence"][0]["id"] == "E2"
        assert wire["evidence_ids"] == ["E1", "E2"]
        return response(
            json.dumps(
                {
                    "summary": "核对接口",
                    "evidence_ids": [alias],
                    "missing_information": [],
                    "risks": [],
                }
            )
        )

    model = ChatCompletionsModel(httpx.MockTransport(handler))
    if alias == "E2":
        assert model.assess("寻源员", context, 1000).assessment.evidence_ids == [
            "catalog:dev-32"
        ]
    else:
        with pytest.raises(ModelResponseError) as exc:
            model.assess("寻源员", context, 1000)
        assert exc.value.diagnostic["response_issue"] == "unknown_evidence"


def test_invalid_response_is_charged_in_shared_budget(env):
    c, db, w = env
    requirement(c, w)

    class Invalid:
        def assess(self, *args):
            raise ModelResponseError(
                "schema_validation",
                {"prompt_tokens": 100, "completion_tokens": 1000},
                "stop",
                ["risks"],
            )

    result = analysis.run(
        db, w, "test", Invalid(), pricing={"input": "1", "output": "2"}
    )
    assert result["tokens"] == 1100 and result["uncertain_usage"] is False
    assert result["cost"] == "0.0021"
    with db.transaction(w) as s:
        assert records(s, w, "model_call")[-1].data["state"] == "invalid_response"
