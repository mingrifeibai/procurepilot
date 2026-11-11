"""模型失效不能越过业务边界；预算和用量覆盖全部角色。"""

from .test_procurement import requirement
from procurepilot import analysis
from procurepilot.models import Assessment, Delegation, ModelReply, QuantityCheck


class FakeModel:
    def __init__(self, missing=False):
        self.calls = 0
        self.missing = missing

    def assess(self, role, context, max_output):
        self.calls += 1
        return ModelReply(
            assessment=Assessment(
                summary=role,
                evidence_ids=context["evidence_ids"][:1],
                missing_information=["需要确认设计岗位显卡"] if self.missing else [],
                risks=[],
            ),
            prompt_tokens=100,
            completion_tokens=50,
        )


def test_model_amount_units_are_explicit_and_wrong_amount_is_rejected(env):
    c, db, wid = env
    requirement(c, wid, budget="8000", quantity=1)
    c.post(f"/api/w/{wid}/plans").raise_for_status()

    class WrongAmount(FakeModel):
        def assess(self, role, context, max_output):
            assert context["requirement"]["budget_cny"] == "8000.00 元"
            assert context["plan"]["total_cny"] == "5800.00 元"
            assert "price_cents" not in context["quotes"][0]
            response = super().assess(role, context, max_output)
            response.assessment.summary = "预算80万，报价58万元，符合要求"
            return response

    rejected = analysis.run(
        db, wid, "tester", WrongAmount(), coordination_mode="single"
    )
    assert rejected["state"] == "waiting_response"
    assert rejected["error"]["response_issue"] == "unsupported_amount"
    assert not rejected["assessments"] and rejected["tokens"] == 150

    class CorrectAmount(WrongAmount):
        def assess(self, *args):
            response = super().assess(*args)
            response.assessment.summary = "预算8,000.00元，报价5800元"
            return response

    restored = analysis.run(db, wid, "tester", CorrectAmount(), task_id=rejected["id"])
    assert restored["state"] == "completed" and restored["tokens"] == 300


def test_model_can_quote_deterministic_line_subtotal_without_plan(env):
    client, store, wid = env
    requirement(client, wid, budget="15000", quantity=2)

    class QuoteReview(FakeModel):
        def __init__(self, subtotal):
            super().__init__()
            self.subtotal = subtotal

        def assess(self, role, context, max_output):
            quote = next(
                item for item in context["quotes"] if item["product_id"] == "dev-32"
            )
            assert quote["line_subtotals"] == [
                {"need_index": 0, "quantity": 2, "subtotal_cny": "11600.00 元"}
            ]
            monitor = next(
                item for item in context["quotes"] if item["product_id"] == "screen-27"
            )
            assert "line_subtotals" not in monitor
            assert context["plan"] is None
            reply = super().assess(role, context, max_output)
            reply.assessment.summary = f"该报价覆盖两台，整行小计{self.subtotal}元"
            reply.assessment.evidence_ids = [quote["id"]]
            return reply

    checked = analysis.run(
        store, wid, "tester", QuoteReview("11,600"), coordination_mode="single"
    )
    assert checked["state"] == "completed" and checked["tokens"] == 150
    rejected = analysis.run(
        store, wid, "tester", QuoteReview("11,700"), coordination_mode="single"
    )
    assert rejected["state"] == "waiting_response" and rejected["tokens"] == 150
    assert rejected["error"]["response_issue"] == "unsupported_amount"


def test_model_gets_actual_split_allocations_instead_of_candidate_subtotals(env):
    client, store, wid = env
    prefix = f"/api/w/{wid}"
    client.post(
        prefix + "/requirements",
        json={
            "title": "研发与设计设备",
            "budget": "150000",
            "delivery_days": 9,
            "needs": [
                {
                    "category": "computer",
                    "quantity": 8,
                    "min_memory_gb": 32,
                    "interface": "usb-c",
                },
                {
                    "category": "computer",
                    "quantity": 4,
                    "min_memory_gb": 64,
                    "interface": "usb-c",
                },
            ],
        },
    ).raise_for_status()
    client.post(
        prefix + "/quotes",
        json={
            "product_id": "design-64",
            "supplier": "备用供应商",
            "unit_price": "9000",
            "stock": 1,
            "delivery_days": 6,
            "source": "fixture://one-design-unit",
        },
    ).raise_for_status()
    plan = client.post(prefix + "/plans").raise_for_status().json()
    assert plan["total_cents"] == 8420000

    class Review(FakeModel):
        def __init__(self, summary):
            super().__init__()
            self.summary = summary

        def assess(self, role, context, max_output):
            assert context["plan"]["need_coverage"] == [
                {"need_index": 0, "required_quantity": 8, "allocated_quantity": 8},
                {"need_index": 1, "required_quantity": 4, "allocated_quantity": 4},
            ]
            assert {
                (line["quantity"], line["subtotal_cny"])
                for line in context["plan"]["lines"]
                if line["need_index"] == 1
            } == {(1, "9000.00 元"), (3, "28800.00 元")}
            assert all("line_subtotals" not in quote for quote in context["quotes"])
            reply = super().assess(role, context, max_output)
            reply.assessment.summary = self.summary
            return reply

    result = analysis.run(
        store,
        wid,
        "tester",
        Review("设计设备分两条报价分配，3台小计28800元，1台小计9000元"),
        coordination_mode="single",
    )
    assert result["state"] == "completed" and result["tokens"] == 150
    rejected = analysis.run(
        store,
        wid,
        "tester",
        Review("方案中的设计设备分配小计38400元"),
        coordination_mode="single",
    )
    assert rejected["state"] == "waiting_response"
    assert rejected["error"]["response_issue"] == "unsupported_amount"


def test_split_plan_fills_missing_independent_reviewer(env):
    client, store, wid = env
    prefix = f"/api/w/{wid}"
    requirement(client, wid, budget="25000", quantity=4)
    client.post(
        prefix + "/quotes",
        json={
            "product_id": "dev-32",
            "supplier": "部分供货",
            "unit_price": "5000",
            "stock": 1,
            "delivery_days": 6,
            "source": "fixture://one-dev-unit",
        },
    ).raise_for_status()
    plan = client.post(prefix + "/plans").raise_for_status().json()
    assert sorted(line["quantity"] for line in plan["lines"]) == [1, 3]

    class MissingQuoteReviewer(FakeModel):
        def assess(self, role, context, max_output):
            reply = super().assess(role, context, max_output)
            if role == "采购协调员":
                assert context["requires_split_review"]
                reply.assessment.delegations = [
                    Delegation(role="寻源员", objective="模型指定的规格任务")
                ]
            elif role == "证据核验员":
                reply.assessment.quantity_checks = [
                    QuantityCheck.model_validate(row)
                    for row in context["plan"]["need_coverage"]
                ]
            elif role == "寻源员":
                assert context["delegated_task"]["objective"] == "模型指定的规格任务"
            else:
                assert context["delegated_task"]["role"] == "报价分析员"
            return reply

    result = analysis.run(
        store, wid, "tester", MissingQuoteReviewer(), coordination_mode="dynamic"
    )
    assert result["state"] == "completed" and result["tokens"] == 600
    assert result["delegation_state"] == "policy_completed"
    assert result["policy_added_roles"] == ["报价分析员"]
    assert result["parallel_batches"][0]["roles"] == ["寻源员", "报价分析员"]


def test_multi_role_shared_usage(env):
    c, db, w = env
    requirement(c, w)
    model = FakeModel()
    r = analysis.run(db, w, "tester", model)
    assert r["state"] == "completed" and model.calls == 3
    assert r["tokens"] == 450 and len(r["assessments"]) == 3
    assert not c.get(f"/api/w/{w}/snapshot").json()["order"]


def test_missing_evidence_not_success(env):
    c, db, w = env
    requirement(c, w)
    assert (
        analysis.run(db, w, "tester", FakeModel(True))["state"]
        == "evidence_insufficient"
    )


def test_budget_checked_before_model_call(env):
    c, db, w = env
    requirement(c, w)
    model = FakeModel()
    r = analysis.run(db, w, "tester", model, max_tokens=1)
    assert r["state"] == "budget_exhausted" and model.calls == 0


def test_failure_is_uncertain_and_never_auto_retried(env):
    c, db, w = env
    requirement(c, w)

    class Broken:
        calls = 0

        def assess(self, *args):
            self.calls += 1
            raise TimeoutError("sensitive provider response must not be exposed")

    model = Broken()
    r = analysis.run(db, w, "tester", model)
    assert r["state"] == "tool_unavailable" and r["uncertain_usage"] is True
    assert model.calls == 1 and "sensitive" not in str(r)


def test_unconfigured_model_does_not_fake_completion(env, monkeypatch):
    c, _, w = env
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    assert c.post(f"/api/w/{w}/analyses").status_code == 503


def test_chat_adapter_contract_and_unknown_citation(monkeypatch):
    import httpx
    import json
    import pytest
    from procurepilot.models import ChatCompletionsModel

    for k, v in {
        "MODEL_BASE_URL": "https://model.test/v1",
        "MODEL_API_KEY": "unit-test-secret",
        "MODEL_NAME": "test-model",
    }.items():
        monkeypatch.setenv(k, v)

    def response(request):
        assert str(request.url) == "https://model.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer unit-test-secret"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "建议",
                                    "evidence_ids": ["q1"],
                                    "missing_information": [],
                                    "risks": [],
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 99, "completion_tokens": 12},
            },
        )

    model = ChatCompletionsModel(httpx.MockTransport(response))
    assert model.assess("核验员", {"evidence_ids": ["q1"]}, 1000).prompt_tokens == 99
    with pytest.raises(ValueError, match="不存在的证据"):
        model.assess("核验员", {"evidence_ids": []}, 1000)
