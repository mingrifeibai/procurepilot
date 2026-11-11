"""Langfuse 验收只以项目返回的同一条合成观察记录为准。"""

import httpx
import pytest

from scripts.verify_langfuse import OBSERVATION, observability, verify


@pytest.fixture
def langfuse_keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.invalid")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "fixture-public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "fixture-secret")
    monkeypatch.delenv("LANGFUSE_PROXY_URL", raising=False)


def test_explicit_langfuse_proxy_is_validated(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PROXY_URL", "http://127.0.0.1:7890")
    assert observability.langfuse_proxy() == "http://127.0.0.1:7890"
    monkeypatch.setenv("LANGFUSE_PROXY_URL", "http://fixture-secret@127.0.0.1:7890")
    with pytest.raises(ValueError) as error:
        observability.langfuse_proxy()
    assert "fixture-secret" not in str(error.value)


def test_check_reads_exact_fixture_by_trace_id(langfuse_keys):
    otlp = pytest.importorskip(
        "opentelemetry.proto.collector.trace.v1.trace_service_pb2"
    )
    trace_id = None

    def handle(request):
        nonlocal trace_id
        assert request.headers["authorization"].startswith("Basic ")
        if request.method == "POST":
            assert request.url.path == "/api/public/otel/v1/traces"
            assert request.headers["x-langfuse-ingestion-version"] == "4"
            payload = otlp.ExportTraceServiceRequest.FromString(request.content)
            fixture = payload.resource_spans[0].scope_spans[0].spans[0]
            assert fixture.name == OBSERVATION
            trace_id = fixture.trace_id.hex()
            return httpx.Response(200)
        assert request.url.path == "/api/public/v2/observations"
        assert request.url.params["traceId"] == trace_id
        assert request.url.params["fields"] == "core,basic"
        assert (
            "fromStartTime" in request.url.params
            and "toStartTime" in request.url.params
        )
        return httpx.Response(
            200, json={"data": [{"traceId": trace_id, "name": OBSERVATION}]}
        )

    result = verify(httpx.MockTransport(handle))
    assert result == {
        "passed": True,
        "trace_id": trace_id,
        "fixture": True,
    }


@pytest.mark.parametrize("failure", ["timeout", "auth"])
def test_send_failure_stops_before_readback(langfuse_keys, failure):
    pytest.importorskip("opentelemetry.exporter.otlp.proto.common.trace_encoder")

    def handle(request):
        assert request.method == "POST"
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture-secret", request=request)
        return httpx.Response(401, text="fixture-secret")

    with pytest.raises(ValueError, match="Langfuse OTLP") as error:
        verify(httpx.MockTransport(handle))
    assert "fixture-secret" not in str(error.value)
