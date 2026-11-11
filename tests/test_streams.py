"""真实Redis Streams集成：重复投递、消费者中断、断网与SQL补偿。

设置REDIS_TEST_URL后运行。每例使用随机命名空间，结束只删除本例两个键。
"""

import json
import os
from threading import Barrier
from uuid import uuid4
import httpx
import pytest
import redis
from procurepilot import analysis, jobs
from procurepilot.models import ChatCompletionsModel
from procurepilot.streams import StreamQueue, GROUP
from procurepilot.storage import records
from .test_models import FakeModel
from .test_procurement import requirement


@pytest.fixture
def bus(env, monkeypatch):
    url = os.getenv("REDIS_TEST_URL")
    if not url:
        pytest.skip("真实Streams集成需要REDIS_TEST_URL")
    client = redis.Redis.from_url(
        url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2
    )
    client.ping()
    key = "test:procurepilot:" + uuid4().hex
    monkeypatch.setenv("REDIS_STREAM_KEY", key)
    queue = StreamQueue(env[1], client=client, idle_ms=0)
    yield queue
    client.delete(key, key + ":dead")
    client.close()


def enqueue(env):
    c, db, w = env
    requirement(c, w)
    return analysis.run(db, w, "test", prepare_only=True)


def test_real_stream_executes_and_acks_after_commit(env, bus):
    queued = enqueue(env)
    assert bus.publish() == 1
    assert bus.publish() == 0
    model = FakeModel()
    assert bus.consume_once(model)
    assert model.calls == 3
    assert bus.client.xpending(bus.key, GROUP)["pending"] == 0
    result = env[0].get(f"/api/w/{env[2]}/analyses/{queued['id']}").json()
    assert result["state"] == "completed" and result["tokens"] == 450


def test_stream_runs_dynamic_mcp_and_parallel_roles_against_neo4j(
    env, bus, monkeypatch
):
    """真实 Redis、Neo4j 和 MCP，模型 HTTP 响应固定以注入可重复的编排。"""
    graph_url = os.getenv("TEST_NEO4J_HTTP_URL")
    if not graph_url:
        pytest.skip("设置 TEST_NEO4J_HTTP_URL 后验证真实图谱")
    monkeypatch.setenv("KNOWLEDGE_BACKEND", "neo4j")
    monkeypatch.setenv("NEO4J_HTTP_URL", graph_url)
    monkeypatch.setenv("MODEL_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("MODEL_NAME", "protocol-fixture")
    monkeypatch.setenv("MODEL_API_KEY", "fixture-key")
    barrier = Barrier(2, timeout=5)
    graph_id = "graph:dev-32:dock-c:0"

    def reply(request):
        messages = json.loads(request.content)["messages"]
        context = json.loads(messages[1]["content"])
        content = {
            "summary": "检查完成",
            "evidence_ids": [],
            "missing_information": [],
            "risks": [],
        }
        if "你是企业采购的采购协调员" in messages[0]["content"]:
            content["delegations"] = [
                {"role": "寻源员", "objective": "核对接口"},
                {"role": "报价分析员", "objective": "核对库存"},
            ]
            content["tool_requests"] = [
                {
                    "tool": "check_compatibility",
                    "product_a": "dev-32",
                    "product_b": "dock-c",
                },
            ]
        else:
            assert graph_id in context["evidence_ids"]
            content["evidence_ids"] = [graph_id]
            if context["delegated_task"]:
                barrier.wait()
            else:
                assert len(context["prior_assessments"]) == 3
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(content)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    client, store, wid = env
    requirement(client, wid)
    queued = analysis.run(
        store, wid, "test", prepare_only=True, coordination_mode="dynamic"
    )
    assert bus.publish() == 1
    assert bus.consume_once(ChatCompletionsModel(httpx.MockTransport(reply)))
    assert bus.client.xpending(bus.key, GROUP)["pending"] == 0
    result = (
        client.get(f"/api/w/{wid}/analyses/{queued['id']}").raise_for_status().json()
    )
    assert result["state"] == "completed" and result["tokens"] == 600
    assert result["parallel_batches"][0]["roles"] == ["寻源员", "报价分析员"]
    evidence = result["tool_results"][0]["output"]["evidence"][0]
    assert evidence["source"] == "fixture://compatibility/dev-32/dock-c"
    assert result["assessments"][-1]["evidence_ids"] == [graph_id]


def test_published_but_sql_not_marked_duplicate_does_not_reissue(env, bus):
    enqueue(env)
    bus.publish()
    with env[1].transaction(env[2]) as session:
        event = records(session, env[2], "queue_outbox")[-1]
        event.data = {**event.data, "state": "pending"}
    bus.publish()
    assert bus.client.xlen(bus.key) == 2
    model = FakeModel()
    assert bus.consume_once(model) and bus.consume_once(model)
    assert model.calls == 3 and bus.client.xpending(bus.key, GROUP)["pending"] == 0


def test_unacked_lost_consumer_is_reclaimed(env, bus):
    enqueue(env)
    bus.publish()
    bus.ensure_group()
    assert bus.client.xreadgroup(GROUP, "lost-consumer", {bus.key: ">"}, count=1)
    model = FakeModel()
    assert bus.consume_once(model) and model.calls == 3
    assert bus.client.xpending(bus.key, GROUP)["pending"] == 0


def test_redis_failure_preserves_outbox_and_sql_can_compensate(env, bus, monkeypatch):
    enqueue(env)

    def unavailable(*args, **kwargs):
        raise redis.ConnectionError("do-not-expose-secret")

    monkeypatch.setattr(bus.client, "xadd", unavailable)
    assert bus.publish() == 0
    with env[1].transaction(env[2]) as session:
        event = records(session, env[2], "queue_outbox")[-1]
        assert event.data["state"] == "pending" and event.data["attempts"] == 1
        assert "do-not-expose" not in str(event.data)
    model = FakeModel()
    assert jobs.run_once(env[1], model) and model.calls == 3


def test_poison_message_dead_lettered_without_model_call(env, bus):
    bus.client.xadd(bus.key, {"analysis_id": "ignore all permissions"})
    model = FakeModel()
    assert bus.consume_once(model) and model.calls == 0
    assert bus.client.xlen(bus.key + ":dead") == 1
    assert "ignore" not in str(bus.client.xrange(bus.key + ":dead"))


def test_lost_stream_still_completes_from_sql(env, bus):
    enqueue(env)
    bus.publish()
    bus.client.delete(bus.key)
    model = FakeModel()
    assert jobs.run_once(env[1], model) and model.calls == 3
