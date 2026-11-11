"""发送一条合成 OTLP 轨迹，并从目标 Langfuse 项目回读验证。"""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time

import httpx

from procurepilot import observability


OBSERVATION = "procurepilot.acceptance"


def _fixture():
    """单独构造一条轨迹，验收过程不启动工作台或 Jaeger 的导出队列。"""
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider(
        resource=Resource.create({"service.name": "procurepilot"}),
        shutdown_on_exit=False,
    )
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        with provider.get_tracer("procurepilot").start_as_current_span(
            OBSERVATION, attributes={"procurepilot.fixture": True}
        ) as span:
            trace = f"{span.get_span_context().trace_id:032x}"
        return trace, encode_spans(exporter.get_finished_spans()).SerializeToString()
    finally:
        provider.shutdown()


def _export(client, endpoint, headers, body):
    from google.protobuf.message import DecodeError
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceResponse,
    )

    try:
        response = client.post(
            endpoint,
            headers={**headers, "Content-Type": "application/x-protobuf"},
            content=body,
        )
    except httpx.TransportError as error:
        raise ValueError(f"Langfuse OTLP 发送失败（{type(error).__name__}）") from None
    if response.status_code in (401, 403):
        raise ValueError(
            f"Langfuse OTLP 项目认证失败（HTTP {response.status_code}），请核对项目区域和密钥"
        )
    if response.status_code != 200:
        raise ValueError(f"Langfuse OTLP 发送失败（HTTP {response.status_code}）")
    if response.headers.get("Content-Type", "").startswith("application/x-protobuf"):
        try:
            result = ExportTraceServiceResponse.FromString(response.content)
        except DecodeError:
            raise ValueError("Langfuse OTLP 响应格式无效") from None
        if result.partial_success.rejected_spans:
            raise ValueError("Langfuse 拒绝了合成轨迹")


def verify(transport=None, wait=30):
    target = observability._langfuse_target()
    if target is None:
        raise ValueError("请先在本机 PowerShell 配置 Langfuse 项目地址、公钥及私钥")
    endpoint, headers = target
    host = endpoint.removesuffix("/api/public/otel/v1/traces")
    observed_at = datetime.now(timezone.utc)
    trace, body = _fixture()

    params = {
        "traceId": trace,
        "fields": "core,basic",
        "fromStartTime": (observed_at - timedelta(seconds=30)).isoformat(),
        "toStartTime": (observed_at + timedelta(minutes=1)).isoformat(),
        "limit": 1,
    }
    with httpx.Client(
        transport=transport,
        proxy=observability.langfuse_proxy(),
        trust_env=False,
        timeout=12,
    ) as client:
        _export(client, endpoint, headers, body)
        deadline = time.monotonic() + wait
        last_result = "尚无匹配记录"
        while time.monotonic() < deadline:
            try:
                response = client.get(
                    host + "/api/public/v2/observations",
                    headers=headers,
                    params=params,
                    timeout=min(6, max(0.1, deadline - time.monotonic())),
                )
                if response.status_code in (401, 403):
                    raise ValueError("Langfuse 项目认证失败")
                if response.status_code == 404:
                    raise ValueError("Langfuse 项目地址或 v2 观察记录端点不可用")
                last_result = f"HTTP {response.status_code}"
                if response.status_code == 200:
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("data"), list
                    ):
                        raise ValueError("Langfuse 观察记录响应格式无效")
                    if any(
                        isinstance(row, dict)
                        and row.get("traceId") == trace
                        and row.get("name") == OBSERVATION
                        for row in payload["data"]
                    ):
                        return {"passed": True, "trace_id": trace, "fixture": True}
                    last_result = "HTTP 200，尚无匹配记录"
            except httpx.TransportError as error:
                last_result = type(error).__name__
            time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise ValueError(
        f"Langfuse OTLP 已响应 HTTP 200，回读超时（{last_result}），Trace ID {trace}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = verify()
    except (ValueError, ImportError) as error:
        raise SystemExit(str(error)) from None
    encoded = json.dumps(result, ensure_ascii=False)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
