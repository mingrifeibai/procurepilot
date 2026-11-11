"""真实 stdio 子进程 MCP 握手、发现、调用及授权边界验收。"""

import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import httpx
from mcp import ClientSession, StdioServerParameters, stdio_client


def test_stdio_protocol_and_write_approval(tmp_path):
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'protocol.db').as_posix()}",
        "DEMO_MODE": "1",
        "PROCUREPILOT_URL": base,
        "PROCUREPILOT_TOKEN": "demo-buyer",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "procurepilot.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(
            base_url=base,
            headers={"Authorization": "Bearer demo-buyer"},
            trust_env=False,
            timeout=2,
        ) as client:
            for _ in range(100):
                try:
                    if client.get("/api/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if process.poll() is not None:
                    raise AssertionError("测试 API 启动失败")
                time.sleep(0.1)
            else:
                raise AssertionError("测试 API 启动超时")
            wid = client.post("/api/workspaces", json={"name": "MCP 协议验收"}).json()[
                "id"
            ]
            assert (
                client.post(
                    f"/api/w/{wid}/requirements",
                    json={
                        "title": "测试",
                        "budget": "20000",
                        "delivery_days": 14,
                        "needs": [
                            {"category": "computer", "quantity": 1, "min_memory_gb": 32}
                        ],
                    },
                ).status_code
                == 201
            )
            pid = client.post(f"/api/w/{wid}/plans").json()["id"]

            async def scenario():
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "procurepilot.mcp_server"],
                    env=env,
                    cwd=str(root),
                )
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        names = {t.name for t in listed.tools}
                        assert {
                            "read_quotes",
                            "create_purchase",
                            "query_operation",
                            "read_attachment",
                        } <= names
                        result = await session.call_tool(
                            "search_products", {"query": "研发"}
                        )
                        assert not result.is_error and "研发" in str(result)
                        params = {
                            "workspace_id": wid,
                            "plan_id": pid,
                            "idempotency_key": "mcp-execute-001",
                        }
                        denied = await session.call_tool("create_purchase", params)
                        assert denied.is_error
                        approved = client.post(
                            f"/api/w/{wid}/plans/{pid}/approve",
                            headers={"Authorization": "Bearer demo-approver"},
                        )
                        assert approved.status_code == 200
                        first = await session.call_tool("create_purchase", params)
                        again = await session.call_tool("create_purchase", params)
                        assert not first.is_error and not again.is_error
                        found = await session.call_tool(
                            "query_operation",
                            {"workspace_id": wid, "idempotency_key": "mcp-execute-001"},
                        )
                        assert not found.is_error and "found" in str(found)
                        return

            asyncio.run(asyncio.wait_for(scenario(), timeout=45))
            assert len(client.get(f"/api/w/{wid}/snapshot").json()["order"]) == 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
