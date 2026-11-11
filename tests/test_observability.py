"""隔离进程验证真实SDK追踪和敏感字段排除，不访问外部采集器。"""

import subprocess
import sys
import textwrap
import pytest


def test_actual_sdk_trace_redacts_body_tokens_and_exception_text():
    pytest.importorskip("opentelemetry.sdk")
    script = textwrap.dedent("""
        import json,tempfile
        from pathlib import Path
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from procurepilot.observability import span
        exporter=InMemorySpanExporter()
        provider=TracerProvider();provider.add_span_processor(SimpleSpanProcessor(exporter));trace.set_tracer_provider(provider)
        try:
            with span('agent.assess',{'agent.role':'test'}):
                raise RuntimeError('do-not-export-exception-secret')
        except RuntimeError:pass
        from fastapi.testclient import TestClient
        from procurepilot.api import create_app
        from procurepilot.storage import Store
        with tempfile.TemporaryDirectory() as directory:
            store=Store('sqlite:///'+(Path(directory)/'trace.db').as_posix())
            with TestClient(create_app(store)) as client:
                response=client.post('/api/workspaces',json={'name':'do-not-export-body-secret'},headers={'Authorization':'Bearer demo-buyer'})
                assert response.status_code==201 and len(response.headers['X-Trace-Id'])==32
                assert client.get('/api/workspaces',headers={'Authorization':'Bearer do-not-export-token-secret'}).status_code==401
            store.engine.dispose()
        spans=exporter.get_finished_spans()
        payload=json.dumps([{'name':s.name,'attributes':dict(s.attributes),'events':[str(e) for e in s.events]} for s in spans])
        assert 'do-not-export' not in payload
        assert any(s.name=='POST /api/workspaces' for s in spans)
        assert spans[0].attributes['error.type']=='RuntimeError'
        print('SDK traces and redaction verified')
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_langfuse_otlp_wire_contract(monkeypatch):
    """本机协议替身验证路径、认证、版本头及可解析的 OTLP 数据。"""
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    script = textwrap.dedent("""
        import os
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
        from procurepilot.observability import configure, flush, span

        requests = []
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = self.rfile.read(int(self.headers['Content-Length']))
                requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, payload))
                self.send_response(200)
                self.end_headers()

        with ThreadingHTTPServer(('127.0.0.1', 0), Receiver) as server:
            runner = Thread(target=server.serve_forever, daemon=True)
            runner.start()
            os.environ['LANGFUSE_HOST'] = 'http://127.0.0.1:' + str(server.server_port)
            os.environ['LANGFUSE_PUBLIC_KEY'] = 'pk-test'
            os.environ['LANGFUSE_SECRET_KEY'] = 'sk-test'
            os.environ['OTEL_EXPORTER_OTLP_TRACES_ENDPOINT'] = os.environ['LANGFUSE_HOST'] + '/jaeger'
            configure()
            with span('agent.assess', {'gen_ai.usage.input_tokens': 17}):
                pass
            assert flush()
            server.shutdown()
        assert len(requests) == 2
        by_path = {path: (headers, payload) for path, headers, payload in requests}
        headers, payload = by_path['/api/public/otel/v1/traces']
        assert headers['authorization'] == 'Basic cGstdGVzdDpzay10ZXN0'
        assert headers['x-langfuse-ingestion-version'] == '4'
        assert 'authorization' not in by_path['/jaeger'][0]
        trace = ExportTraceServiceRequest.FromString(payload)
        generation = trace.resource_spans[0].scope_spans[0].spans[0]
        assert generation.name == 'agent.assess'
        assert generation.attributes[0].value.int_value == 17
        assert b'sk-test' not in payload
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
