"""Neo4j Query API适配：固定Cypher、参数绑定、有界结果与来源契约。

目录图谱使用独立scope，不存放工作区附件、偏好或模型密钥。读取失败必须
阻止规划和执行，不能悄悄切回演示快照。环境变量仅由部署者配置。
"""

import hashlib
import json
import os
import re
import httpx
from pydantic import Field, model_validator
from typing import Literal
from .domain import StrictModel
from .settings import setting
from urllib.parse import urlparse


class Relation(StrictModel):
    products: list[str] = Field(min_length=2, max_length=2)
    state: Literal["verified", "unknown", "incompatible", "conflict", "revoked"]
    source: str = Field(min_length=1, max_length=500)
    active: bool = True
    simulated: bool = True

    @model_validator(mode="after")
    def distinct(self):
        if self.products[0] == self.products[1]:
            raise ValueError("兼容关系必须连接两个不同产品")
        return self


def query(statement, parameters, transport=None):
    """HTTP成功仍可能携带Cypher错误；两层都检查，错误正文不外泄。"""
    from .service import fail

    database = os.getenv("NEO4J_DATABASE", "neo4j")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", database):
        fail("Neo4j数据库名无效", 503)
    url = setting("NEO4J_HTTP_URL", "").rstrip("/")
    local_demo = os.getenv("DEMO_MODE", "1") == "1" and urlparse(url).hostname in (
        "127.0.0.1",
        "localhost",
    )
    user = os.getenv("NEO4J_USER", "neo4j" if local_demo else None)
    password = os.getenv(
        "NEO4J_PASSWORD", "local-demo-graph-only" if local_demo else None
    )
    if not url or not user or not password:
        fail("请配置Neo4j地址、用户和密码", 503)
    try:
        with httpx.Client(timeout=5, trust_env=False, transport=transport) as client:
            response = client.post(
                f"{url}/db/{database}/query/v2",
                auth=(user, password),
                json={"statement": statement, "parameters": parameters},
            )
            response.raise_for_status()
            if len(response.content) > 1_000_000:
                raise ValueError("oversized")
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("errors"):
                raise ValueError("query_failed")
            return payload["data"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        fail("图谱服务不可用或结果无效，请检查服务端配置", 503)


def relations(transport=None):
    """限定命名空间与最大关系数量；超过上限拒绝，避免截断遗漏冲突关系。"""
    from .service import fail

    backend = setting("KNOWLEDGE_BACKEND", "fixture")
    if backend == "fixture":
        from .knowledge import COMPATIBILITY

        return COMPATIBILITY
    if backend != "neo4j":
        fail("KNOWLEDGE_BACKEND必须是fixture或neo4j", 503)
    result = query(
        "MATCH (a:ProcureProduct)-[r:PROCURE_COMPATIBILITY]->(b:ProcureProduct) "
        "WHERE r.scope = $scope AND a.scope = $scope AND b.scope = $scope "
        "RETURN {products:[a.product_id,b.product_id],state:r.state,source:r.source,"
        "active:coalesce(r.active,true),simulated:coalesce(r.simulated,true)} AS relation "
        "ORDER BY a.product_id,b.product_id,r.source LIMIT 1001",
        {"scope": setting("NEO4J_GRAPH_SCOPE", "procurepilot-fixtures-v1")},
        transport,
    )
    try:
        if result["fields"] != ["relation"] or len(result["values"]) > 1000:
            raise ValueError()
        if any(not isinstance(row, list) or len(row) != 1 for row in result["values"]):
            raise ValueError()
        return [
            Relation.model_validate(row[0]).model_dump() for row in result["values"]
        ]
    except (ValueError, KeyError, TypeError):
        fail("图谱关系不符合来源契约", 503)


def fingerprint(rows, *, legacy=False):
    """稳定排序后对完整关系快照签名，图谱变化会使原方案审批失效。"""
    canonical = sorted(
        json.dumps(
            r
            if legacy
            else {
                **r,
                "products": sorted(r["products"]),
                "active": r.get("active", True),
                "simulated": r.get("simulated", True),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for r in rows
    )
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False).encode()
    ).hexdigest()


def matches_plan_snapshot(rows, saved):
    """兼容升级前的方案，同时保留图谱变更后的审批拦截。"""
    current = fingerprint(rows)
    if saved is None:
        # 旧版只使用固定目录。图谱与该目录语义一致时，旧审批可以继续复用。
        from .knowledge import COMPATIBILITY

        return current == fingerprint(COMPATIBILITY)
    return saved in (current, fingerprint(rows, legacy=True))


def seed_fixtures():
    """显式维护命令导入模拟关系，MERGE按scope及来源幂等，不导入用户资料。"""
    from .knowledge import COMPATIBILITY

    return query(
        "UNWIND $rows AS row "
        "MERGE (a:ProcureProduct {scope:$scope,product_id:row.products[0]}) "
        "MERGE (b:ProcureProduct {scope:$scope,product_id:row.products[1]}) "
        "MERGE (a)-[r:PROCURE_COMPATIBILITY {scope:$scope,source:row.source}]->(b) "
        "SET r.state=row.state,r.active=true,r.simulated=true RETURN count(r) AS imported",
        {
            "scope": setting("NEO4J_GRAPH_SCOPE", "procurepilot-fixtures-v1"),
            "rows": COMPATIBILITY,
        },
    )
