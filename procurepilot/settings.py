"""部署设置：进程环境优先，允许本地演示的非敏感服务配置文件。

MODEL_API_KEY等模型凭据不从文件加载。服务配置文件不接受认证字段，
带凭据的Redis连接必须使用进程环境；修改文件后需重载服务。
"""

import json
import os
from pathlib import Path
from urllib.parse import urlparse


def setting(name, default=None):
    """只解析服务连接与模式；模型密钥和访问令牌没有文件读取路径。"""
    if name in os.environ:
        return os.environ[name]
    path = Path(__file__).resolve().parents[1] / ".local/services.json"
    if not path.exists():
        return default
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    allowed = {
        "redis_url",
        "knowledge_backend",
        "neo4j_http_url",
        "neo4j_graph_scope",
        "search_backend",
        "opensearch_url",
        "otel_exporter_otlp_traces_endpoint",
    }
    if set(payload) - allowed:
        raise ValueError("本地服务设置包含不允许的字段")
    return payload.get(name.lower(), default)


def redis_url():
    if "REDIS_URL" in os.environ:
        return os.environ["REDIS_URL"]
    value = setting("REDIS_URL")
    if value:
        parsed = urlparse(value)
        if (
            parsed.scheme != "redis"
            or parsed.hostname not in ("127.0.0.1", "localhost")
            or parsed.username
            or parsed.password
        ):
            raise ValueError("本地文件仅允许不含凭据的本机Redis；其他部署使用环境变量")
    return value


def search_backend():
    value = setting("SEARCH_BACKEND", "local")
    if value not in ("local", "opensearch"):
        from .service import fail

        fail("SEARCH_BACKEND必须为local或opensearch", 503)
    return value
