"""真实 PostgreSQL 与 S3 上验证两个独立 API 进程的附件和采购一致性。"""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from procurepilot.objects import S3Objects


@contextmanager
def replica(env, directory, name):
    """子进程只监听回环地址，各自使用独立的本地对象目录。"""
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
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
        env={**env, "OBJECT_ROOT": str(directory / name)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=25
        ) as client:
            deadline = time.monotonic() + 35
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    status = client.get("/api/health", timeout=2)
                    if status.status_code == 200:
                        assert status.json()["mode"] == "configured"
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.4)
            else:
                raise RuntimeError(f"API fixture {name} did not start")
            yield client, process.pid
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def run():
    url = make_url(os.environ["TEST_DATABASE_URL"])
    if url.get_backend_name() != "postgresql":
        raise ValueError("此验收需要独立 PostgreSQL 测试连接")
    namespace = "test_replica_" + uuid4().hex
    isolated = url.update_query_dict(
        {"options": f"-csearch_path={namespace}", "connect_timeout": "3"}
    )
    buyer, approver = token_urlsafe(24), token_urlsafe(24)
    target = {
        "OBJECT_S3_ENDPOINT": os.environ["TEST_OBJECT_S3_ENDPOINT"],
        "OBJECT_S3_BUCKET": os.environ["TEST_OBJECT_S3_BUCKET"],
        "OBJECT_S3_PREFIX": namespace,
    }
    environment = {
        **os.environ,
        **target,
        "OBJECT_BACKEND": "s3",
        "DEMO_MODE": "0",
        "DATABASE_URL": isolated.render_as_string(hide_password=False),
        "REQUIRE_POSTGRES": "1",
        "MODEL_WORKER_MODE": "disabled",
        "API_TOKENS_JSON": json.dumps(
            {
                buyer: {"role": "buyer", "workspaces": ["*"]},
                approver: {"role": "approver", "workspaces": ["*"]},
            }
        ),
    }
    admin = create_engine(url, connect_args={"connect_timeout": 3})
    os.environ.update(target)
    objects = S3Objects()
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{namespace}"'))
        with TemporaryDirectory() as workspace, ExitStack() as stack:
            first, pid_a = stack.enter_context(
                replica(environment, Path(workspace), "first")
            )
            second, pid_b = stack.enter_context(
                replica(environment, Path(workspace), "second")
            )
            for client in (first, second):
                client.headers["Authorization"] = f"Bearer {buyer}"
            created = (
                first.post("/api/workspaces", json={"name": "双实例合成验收"})
                .raise_for_status()
                .json()
            )
            prefix = f"/api/w/{created['id']}"
            content = "双实例原始报价，验收使用。\n".encode()
            attachment = (
                first.post(
                    prefix + "/attachments",
                    json={
                        "filename": "双实例.txt",
                        "content_base64": base64.b64encode(content).decode(),
                    },
                )
                .raise_for_status()
                .json()
            )
            download = second.get(
                f"{prefix}/attachments/{attachment['id']}/original"
            ).raise_for_status()
            assert download.content == content and pid_a != pid_b
            first.post(
                prefix + "/requirements",
                json={
                    "title": "双实例电脑采购",
                    "budget": "8000",
                    "delivery_days": 9,
                    "needs": [
                        {"category": "computer", "quantity": 1, "min_memory_gb": 32}
                    ],
                },
            ).raise_for_status()
            plan = second.post(prefix + "/plans").raise_for_status().json()
            plan_url = prefix + f"/plans/{plan['id']}"
            first.post(
                plan_url + "/approve",
                headers={
                    "Authorization": f"Bearer {approver}",
                },
            ).raise_for_status()

            def execute(client):
                return (
                    client.post(
                        plan_url + "/execute",
                        json={
                            "idempotency_key": "replica-procurement-test",
                        },
                    )
                    .raise_for_status()
                    .json()["id"]
                )

            with ThreadPoolExecutor(max_workers=2) as workers:
                order_ids = list(workers.map(execute, (first, second)))
            snapshot = second.get(prefix + "/snapshot").raise_for_status().json()
            quote_id = plan["lines"][0]["quote_id"]
            stock = next(
                row["stock"] for row in snapshot["quote"] if row["id"] == quote_id
            )
            assert (
                len(set(order_ids))
                == len(snapshot["order"])
                == len(snapshot["outbox"])
                == 1
            )
            assert stock == 99
        return {
            "passed": True,
            "distinct_api_processes": 2,
            "shared_local_volume": False,
            "s3_roundtrip": True,
            "order_attempts": 2,
            "orders": 1,
            "outbox_events": 1,
            "remaining_stock": stock,
            "model_calls": 0,
        }
    finally:
        try:
            with closing(objects._client()) as client:
                keys = client.list_objects_v2(
                    Bucket=objects.bucket, Prefix=namespace + "/"
                )
                for item in keys.get("Contents", []):
                    client.delete_object(Bucket=objects.bucket, Key=item["Key"])
        finally:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{namespace}" CASCADE'))
            admin.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run()
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
