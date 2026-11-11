"""可选OpenTelemetry追踪。仅导出明确白名单属性，不记录正文、密钥或异常文本。"""

from base64 import b64encode
from contextlib import contextmanager
import os
from threading import Lock
from urllib.parse import urlsplit

from .settings import setting

_provider = None
_lock = Lock()


def langfuse_proxy():
    """仅为 Langfuse 使用显式代理，避免将本机采集器流量一并转发。"""
    value = os.getenv("LANGFUSE_PROXY_URL", "").strip()
    if not value:
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme in ("http", "https")
            and url.hostname
            and url.port != 0
            and not (url.username or url.password or url.query or url.fragment)
            and url.path in ("", "/")
        ):
            return value
    except ValueError:
        pass
    raise ValueError(
        "LANGFUSE_PROXY_URL 需填写 HTTP(S) 代理地址，例如 http://127.0.0.1:7890"
    )


def _langfuse_target():
    """启用后将同一份脱敏轨迹送往 Langfuse，凭据只从进程环境获取。"""
    host = os.getenv("LANGFUSE_HOST", "").strip().rstrip("/")
    if not host:
        return None
    url = urlsplit(host)
    loopback = url.hostname in ("localhost", "127.0.0.1", "::1")
    if (
        not url.hostname
        or (url.scheme != "https" and not (url.scheme == "http" and loopback))
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("LANGFUSE_HOST 需使用 HTTPS 或本地回环地址")
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        raise ValueError("启用 Langfuse 需配置项目公钥及私钥")
    credentials = b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
    return (
        host + "/api/public/otel/v1/traces",
        {"Authorization": "Basic " + credentials, "x-langfuse-ingestion-version": "4"},
    )


def configure():
    global _provider
    endpoint = setting("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    langfuse = _langfuse_target()
    proxy = langfuse_proxy() if langfuse else None
    targets = [(endpoint, None)] if endpoint else []
    if langfuse:
        targets.append(langfuse)
    if not targets:
        return
    with _lock:
        if _provider is not None:
            return
        import requests
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        provider = TracerProvider(
            resource=Resource.create({"service.name": "procurepilot"})
        )
        for url, headers in targets:
            session = requests.Session()
            session.trust_env = False
            if headers and proxy:
                session.proxies.update({"http": proxy, "https": proxy})
            provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=url,
                        headers=headers,
                        timeout=12 if headers else 2,
                        session=session,
                    ),
                    max_queue_size=1024,
                    schedule_delay_millis=1000,
                )
            )
        trace.set_tracer_provider(provider)
        _provider = provider


@contextmanager
def span(name, attributes=None):
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

    with trace.get_tracer("procurepilot").start_as_current_span(
        name,
        attributes=attributes or {},
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except Exception as error:
            current.set_attribute("error.type", type(error).__name__)
            current.set_status(Status(StatusCode.ERROR))
            raise


def trace_id():
    from opentelemetry import trace

    context = trace.get_current_span().get_span_context()
    return f"{context.trace_id:032x}" if context.is_valid else None


def flush():
    return _provider.force_flush(timeout_millis=3000) if _provider else True
