"""模型分析的只读 MCP 服务。每轮只绑定一个工作区的证据快照。"""

import json
from typing import Any

import anyio
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from . import graph, knowledge, retrieval
from .observability import span
from .service import CATALOG
from .storage import records, view


def _arguments(requests):
    """规范化无序的兼容组合，相同请求在一轮内仅执行一次。"""
    unique = {}
    for request in requests:
        args = dict(request)
        if args["tool"] == "check_compatibility":
            args["product_a"], args["product_b"] = sorted(
                (args["product_a"], args["product_b"])
            )
        unique[json.dumps(args, sort_keys=True, ensure_ascii=False)] = args
    return list(unique.values())


async def _call(requests, artifacts, relations):
    server = MCPServer("ProcurePilot analysis")

    @server.tool(structured_output=True)
    def search_products(query: str) -> dict[str, Any]:
        """在公开的演示目录中按名称、型号和类别查找产品。"""
        needle = query.casefold()
        matches = [
            {"id": f"catalog:{pid}", "product": product, "simulated": True}
            for pid, product in CATALOG.items()
            if any(
                needle in term.casefold()
                for term in (pid, product["name"], product["category"])
            )
        ]
        return {"evidence": matches[:3], "total": len(matches)}

    @server.tool(structured_output=True)
    def check_compatibility(product_a: str, product_b: str) -> dict[str, Any]:
        """核验两个不同的目录产品，缺少记录或来源冲突时保持未知。"""
        if relations is None:
            return {"state": "unavailable", "evidence": []}
        if product_a == product_b or any(
            p not in CATALOG for p in (product_a, product_b)
        ):
            return {"state": "unknown", "evidence": []}
        matches = knowledge.selected_evidence([product_a, product_b], relations)
        return {
            "state": "verified"
            if knowledge.known_compatible(product_a, product_b, matches)
            else "unknown",
            "total": len(matches),
            "evidence": [
                {
                    "simulated": True,
                    **row,
                    "id": f"graph:{product_a}:{product_b}:{index}",
                }
                for index, row in enumerate(matches[:3])
            ],
        }

    @server.tool(structured_output=True)
    def search_attachments(query: str) -> dict[str, Any]:
        """在本次运行授权的附件版本中重新检索有限原文片段。"""
        hits = retrieval.search(artifacts, query, limit=2)
        return {
            "evidence": [
                {
                    "id": hit["source"],
                    "artifact_id": hit["artifact_id"],
                    "offset": hit["offset"],
                    **retrieval.focus_excerpt(hit, query),
                    "content_sha256": hit["content_sha256"],
                }
                for hit in hits
            ],
            "method": retrieval.METHOD,
        }

    results = {}
    async with Client(server, mode="legacy", read_timeout_seconds=10) as client:

        async def invoke(index, request):
            name = request["tool"]
            args = {key: val for key, val in request.items() if key != "tool"}
            result = {
                "tool": name,
                "arguments": args,
                "state": "unavailable",
                "untrusted": True,
            }
            with span("agent.mcp.read", {"mcp.tool.name": name}):
                try:
                    response = await client.call_tool(name, args)
                    payload = response.structured_content
                    if (
                        not response.is_error
                        and isinstance(payload, dict)
                        and payload.get("state") != "unavailable"
                        and len(json.dumps(payload, ensure_ascii=False)) <= 2600
                    ):
                        result = {**result, "state": "completed", "output": payload}
                except Exception:
                    # 外部服务或 SDK 错误可能带有连接细节，只保留固定状态。
                    pass
            results[index] = result

        async with anyio.create_task_group() as tasks:
            for index, request in enumerate(requests):
                tasks.start_soon(invoke, index, request)
    return [results[index] for index in range(len(requests))]


def execute(store, wid, manifest, requests):
    """取授权范围的证据后释放数据库锁，再执行只读工具协议。"""
    requests = _arguments(requests)
    tools = {request["tool"] for request in requests}
    artifacts = []
    if "search_attachments" in tools:
        allowed = set(manifest)
        with store.transaction(wid) as session:
            artifacts = [
                view(record)
                for record in records(session, wid, "artifact")
                if record.id in allowed
            ]
    relations = None
    if "check_compatibility" in tools:
        from fastapi import HTTPException

        try:
            relations = graph.relations()
        except HTTPException:
            # 图谱故障隔离在兼容性工具中，其他证据仍然可查。
            pass
    return anyio.run(_call, requests, artifacts, relations)
