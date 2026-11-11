"""动态分析的 MCP 协议、并发预算及失败后的检查点。"""

from threading import Barrier

from fastapi import HTTPException

from procurepilot import analysis, analysis_tools, jobs
from procurepilot.models import (
    CatalogLookup,
    CompatibilityLookup,
    Delegation,
    ModelResponseError,
)
from procurepilot.storage import add, records

from .test_models import FakeModel
from .test_procurement import requirement


def test_on_demand_catalog_api_requires_a_real_tool_citation(env, monkeypatch):
    client, store, wid = env
    requirement(client, wid)
    monkeypatch.setenv("MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("MODEL_API_KEY", "fixture-key")
    monkeypatch.setenv("MODEL_NAME", "fixture-model")

    class CatalogModel(FakeModel):
        def __init__(self, lookup, final_gap=False):
            super().__init__()
            self.lookup = lookup
            self.final_gap = final_gap

        def assess(self, role, context, max_output):
            assert context["product_evidence"] == []
            reply = super().assess(role, context, max_output)
            if role == "采购协调员":
                assert "catalog:dev-32" not in context["evidence_ids"]
                reply.assessment.delegations = []
                reply.assessment.missing_information = ["目录规格待检索"]
                reply.assessment.tool_requests = (
                    [CatalogLookup(tool="search_products", query="dev-32")]
                    if self.lookup
                    else []
                )
            elif self.lookup:
                assert "catalog:dev-32" in context["evidence_ids"]
                reply.assessment.evidence_ids = ["catalog:dev-32"]
                if self.final_gap:
                    reply.assessment.missing_information = ["供应商交期待确认"]
            return reply

    prefix = f"/api/w/{wid}"
    for lookup, final_gap in ((False, False), (True, False), (True, True)):
        payload = {
            "idempotency_key": f"catalog-mode-{lookup}-{final_gap}",
            "coordination_mode": "dynamic",
            "catalog_mode": "on_demand",
        }
        queued = (
            client.post(prefix + "/analysis-jobs", json=payload)
            .raise_for_status()
            .json()
        )
        conflicting = {**payload, "catalog_mode": "preloaded"}
        assert (
            client.post(prefix + "/analysis-jobs", json=conflicting).status_code == 409
        )
        assert jobs.run_once(
            store, CatalogModel(lookup, final_gap), target=(wid, queued["id"])
        )
        result = (
            client.get(prefix + "/analyses/" + queued["id"]).raise_for_status().json()
        )
        assert result["catalog_mode"] == "on_demand" and result["tokens"] == 300
        if final_gap:
            assert result["state"] == "evidence_insufficient"
            with store.transaction(wid) as session:
                questions = [
                    row.data["question"]
                    for row in records(session, wid, "clarification")
                    if row.data["analysis_id"] == queued["id"]
                ]
            assert questions == ["供应商交期待确认"]
        elif lookup:
            assert result["state"] == "completed"
            assert result["tool_results"][0]["tool"] == "search_products"
        else:
            assert result["state"] == "evidence_insufficient" and result["evidence_gap"]


class ParallelModel(FakeModel):
    def __init__(self, fail_once=False, synchronize=True):
        super().__init__()
        self.barrier = Barrier(2, timeout=5) if synchronize else None
        self.fail_once = fail_once

    def assess(self, role, context, max_output):
        result = super().assess(role, context, max_output)
        if role == "采购协调员":
            result.assessment.delegations = [
                Delegation(role="寻源员", objective="核对设备参数"),
                Delegation(role="报价分析员", objective="核对交期"),
            ]
            result.assessment.tool_requests = [
                CompatibilityLookup(
                    tool="check_compatibility", product_a=a, product_b=b
                )
                for a, b in [("dev-32", "dock-c"), ("dock-c", "dev-32")]
            ]
        else:
            evidence_id = "graph:dev-32:dock-c:0"
            assert evidence_id in context["evidence_ids"]
            result.assessment.evidence_ids = [evidence_id]
            if role == "证据核验员":
                assert [row["role"] for row in context["prior_assessments"]] == [
                    "采购协调员",
                    "寻源员",
                    "报价分析员",
                ]
            else:
                assert not context["prior_assessments"]
                if self.barrier:
                    self.barrier.wait()
        if role == "报价分析员" and self.fail_once:
            self.fail_once = False
            raise ModelResponseError(
                "output_truncated",
                {"prompt_tokens": 100, "completion_tokens": 50},
                "length",
            )
        return result


def test_parallel_specialists_and_mcp_results_are_checkpointed(env):
    client, store, wid = env
    requirement(client, wid)
    model = ParallelModel(fail_once=True)
    stopped = analysis.run(store, wid, "buyer", model, coordination_mode="dynamic")
    assert stopped["state"] == "waiting_response" and stopped["tokens"] == 450
    assert [row["role"] for row in stopped["assessments"]] == ["采购协调员", "寻源员"]
    assert len(stopped["tool_results"]) == 1
    assert stopped["tool_results"][0]["output"]["state"] == "verified"
    with store.transaction(wid) as session:
        calls = records(session, wid, "model_call")
        parallel_ids = [
            row.data["parallel_batch_id"]
            for row in calls
            if row.data["parallel_batch_id"]
        ]
        assert len(parallel_ids) == 2 and len(set(parallel_ids)) == 1

    model.barrier = None
    finished = analysis.run(store, wid, "buyer", model, task_id=stopped["id"])
    assert finished["state"] == "completed" and finished["tokens"] == 750
    assert finished["steps"] == 4 and finished["attempts"] == 5
    with store.transaction(wid) as session:
        assert len(records(session, wid, "tool_call")) == 1
        assert not records(session, wid, "order")


def test_parallel_reservation_respects_remaining_steps(env):
    client, store, wid = env
    requirement(client, wid)
    done = analysis.run(
        store,
        wid,
        "buyer",
        ParallelModel(synchronize=False),
        max_steps=2,
        coordination_mode="dynamic",
    )
    assert done["state"] == "budget_exhausted"
    assert done["attempts"] == 2 and done["tokens"] == 300


def test_mcp_attachment_search_does_not_expand_manifest(env):
    _, store, wid = env
    with store.transaction(wid) as session:
        allowed = add(
            session,
            wid,
            "artifact",
            {"text": "标记 TARGET 可引用", "filename": "allowed"},
        )
        add(
            session,
            wid,
            "artifact",
            {"text": "标记 TARGET 不可引用", "filename": "hidden"},
        )
    results = analysis_tools.execute(
        store, wid, [allowed.id], [{"tool": "search_attachments", "query": "TARGET"}]
    )
    assert results[0]["state"] == "completed"
    matches = results[0]["output"]["evidence"]
    assert [item["artifact_id"] for item in matches] == [allowed.id]


def test_mcp_excerpt_keeps_the_match_and_original_character_offsets(env):
    client, store, wid = env
    target = "BC-73 电源板须通过绝缘等级 III 验收。"
    text = "材料留档😀" * 101 + target + "补充说明" * 10
    with store.transaction(wid) as session:
        artifact = add(
            session,
            wid,
            "artifact",
            {"text": text, "filename": "technical.txt", "characters": len(text)},
        )
    result = analysis_tools.execute(
        store,
        wid,
        [artifact.id],
        [{"tool": "search_attachments", "query": "BC-73 绝缘等级"}],
    )[0]
    evidence = result["output"]["evidence"][0]
    assert target in evidence["preview"] and 0 < evidence["preview_offset"]
    assert evidence["id"] == f"artifact:{artifact.id}:offset:0"
    length = evidence["preview_end"] - evidence["preview_offset"]
    original = (
        client.get(
            f"/api/w/{wid}/attachments/{artifact.id}",
            params={"offset": evidence["preview_offset"], "limit": length},
        )
        .raise_for_status()
        .json()
    )
    assert original["text"] == evidence["preview"] and length <= 400


def test_graph_failure_is_visible_with_known_model_usage(env, monkeypatch):
    client, store, wid = env
    requirement(client, wid)

    def unavailable():
        raise HTTPException(503, "graph-test-secret")

    class RequestsGraph(FakeModel):
        def assess(self, role, context, max_output):
            result = super().assess(role, context, max_output)
            if role == "采购协调员":
                result.assessment.delegations = []
                result.assessment.tool_requests = [
                    CompatibilityLookup(
                        tool="check_compatibility",
                        product_a="dev-32",
                        product_b="dock-c",
                    )
                ]
            return result

    monkeypatch.setattr(analysis_tools.graph, "relations", unavailable)
    finished = analysis.run(
        store, wid, "buyer", RequestsGraph(), coordination_mode="dynamic"
    )
    assert finished["state"] == "tool_unavailable" and not finished["uncertain_usage"]
    assert finished["tokens"] == 300
    assert finished["tool_results"][0]["state"] == "unavailable"
    assert "graph-test-secret" not in str(finished)
