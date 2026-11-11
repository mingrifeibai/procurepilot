"""图谱契约、冲突裁决、服务失败及审批绑定的独立边界测试。"""

import httpx
import pytest
from fastapi import HTTPException
from procurepilot import graph, service
from procurepilot.knowledge import known_compatible
from .test_procurement import prepared, execute


def configure(monkeypatch):
    for key, value in {
        "KNOWLEDGE_BACKEND": "neo4j",
        "NEO4J_HTTP_URL": "http://graph.test:7474",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "test-secret",
    }.items():
        monkeypatch.setenv(key, value)


def test_conflicting_or_revoked_relation_cannot_be_overridden_by_verified():
    good = {"products": ["a", "b"], "state": "verified", "source": "fixture://a"}
    assert known_compatible("a", "b", [good])
    for state in ("conflict", "revoked", "unknown", "incompatible"):
        bad = {**good, "state": state, "source": "fixture://b"}
        assert not known_compatible("a", "b", [good, bad])
        assert known_compatible("a", "b", [good, {**bad, "active": False}])


def test_graph_protocol_parameters_and_relation_contract(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("NEO4J_GRAPH_SCOPE", "scope' RETURN password")

    def handler(request):
        import json

        payload = json.loads(request.content)
        assert payload["parameters"]["scope"] not in payload["statement"]
        assert request.url.path == "/db/neo4j/query/v2"
        return httpx.Response(
            202,
            json={
                "data": {
                    "fields": ["relation"],
                    "values": [
                        [
                            {
                                "products": ["a", "b"],
                                "state": "verified",
                                "source": "fixture://a",
                            }
                        ]
                    ],
                }
            },
        )

    result = graph.relations(httpx.MockTransport(handler))
    assert result[0]["active"] and result[0]["simulated"]


@pytest.mark.parametrize(
    "payload",
    [
        {"errors": [{"message": "secret database response"}]},
        {"data": {"fields": ["relation"], "values": [[]]}},
        {
            "data": {
                "fields": ["relation"],
                "values": [
                    [{"products": ["a", "b"], "state": "verified", "source": ""}]
                ],
            }
        },
        {"data": {"fields": ["relation"], "values": [[{}]] * 1001}},
    ],
)
def test_invalid_graph_data_fails_closed(monkeypatch, payload):
    configure(monkeypatch)
    with pytest.raises(HTTPException) as error:
        graph.relations(
            httpx.MockTransport(lambda _: httpx.Response(202, json=payload))
        )
    assert error.value.status_code == 503 and "secret" not in str(error.value.detail)


def test_graph_outage_does_not_fallback_to_fixtures(monkeypatch):
    configure(monkeypatch)
    with pytest.raises(HTTPException) as error:
        graph.relations(httpx.MockTransport(lambda _: httpx.Response(503)))
    assert error.value.status_code == 503


def test_graph_revision_invalidates_approved_plan(env, monkeypatch):
    plan = prepared(env)
    monkeypatch.setattr(service, "graph_relations", lambda: [])
    response = execute(env[0], env[2], plan)
    assert response.status_code == 409
    assert not env[0].get(f"/api/w/{env[2]}/snapshot").json()["order"]


def test_fingerprint_independent_of_result_order():
    rows = [
        {"products": ["a", "b"], "source": "x"},
        {"products": ["c", "d"], "source": "y"},
    ]
    assert graph.fingerprint(rows) == graph.fingerprint(rows[::-1])


def test_legacy_plan_requires_same_fixture_snapshot():
    from procurepilot.knowledge import COMPATIBILITY

    assert graph.matches_plan_snapshot(COMPATIBILITY, None)
    assert graph.matches_plan_snapshot(
        COMPATIBILITY, graph.fingerprint(COMPATIBILITY, legacy=True)
    )
    changed = [{**COMPATIBILITY[0], "state": "conflict"}, *COMPATIBILITY[1:]]
    assert not graph.matches_plan_snapshot(changed, None)
    assert not graph.matches_plan_snapshot(
        changed, graph.fingerprint(COMPATIBILITY, legacy=True)
    )
