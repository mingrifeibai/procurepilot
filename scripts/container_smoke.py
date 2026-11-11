"""对隔离的演示容器运行采购闭环，验证迁移、鉴权、静态页面和事务执行。

会创建带随机标识的合成工作区及本地模拟订单，不调用模型或真实供应商。
仅允许供应商标记为 simulated 的演示环境；建议在临时容器上运行。
"""

import argparse
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4


def verify(base, *, buyer="demo-buyer", approver="demo-approver", mode="demo"):
    opener = build_opener(ProxyHandler({}))

    def request(path, body=None, token=buyer, expected=200):
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        }
        payload = None if body is None else json.dumps(body).encode()
        try:
            response = opener.open(
                Request(base + path, data=payload, headers=headers), timeout=10
            )
        except HTTPError as exc:
            assert exc.code == expected, (path, exc.code, expected)
            return None
        with response:
            assert response.status == expected, (path, response.status, expected)
            return json.load(response)

    # 新容器需要首次迁移；等待仅围绕健康接口，不重试业务写入。
    deadline = time.monotonic() + 60
    while True:
        try:
            health = request("/api/health")
            break
        except (URLError, TimeoutError, ConnectionError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
    assert health["supplier"] == "simulated" and health["mode"] == mode
    request("/api/workspaces", token="invalid-smoke-token", expected=401)
    with opener.open(base + "/", timeout=10) as page:
        assert page.status == 200 and b"/assets/" in page.read()
    workspace = request(
        "/api/workspaces", {"name": "容器合成验收 " + uuid4().hex[:8]}, expected=201
    )
    wid = workspace["id"]
    prefix = f"/api/w/{wid}"
    request(
        prefix + "/requirements",
        {
            "title": "容器验收电脑",
            "budget": "8000",
            "delivery_days": 14,
            "needs": [
                {
                    "category": "computer",
                    "quantity": 1,
                    "min_memory_gb": 32,
                    "interface": "usb-c",
                }
            ],
        },
        expected=201,
    )
    plan = request(prefix + "/plans", {}, expected=201)
    assert plan["status"] == "feasible" and plan["total_cents"] == 580000
    pid = plan["id"]
    execution = {"idempotency_key": "container-smoke-execute"}
    request(prefix + f"/plans/{pid}/execute", execution, expected=403)
    request(prefix + f"/plans/{pid}/approve", {}, token=approver)
    order = request(prefix + f"/plans/{pid}/execute", execution)
    assert request(prefix + f"/plans/{pid}/execute", execution)["id"] == order["id"]
    snapshot = request(prefix + "/snapshot")
    assert len(snapshot["order"]) == 1 and len(snapshot["outbox"]) == 1
    assert not snapshot["analysis"] and not snapshot["model_call"]
    quote = next(
        q for q in snapshot["quote"] if q["id"] == plan["lines"][0]["quote_id"]
    )
    assert quote["stock"] == 99
    request(
        prefix + f"/orders/{order['id']}/receipts",
        {
            "quote_id": quote["id"],
            "quantity": 1,
            "accepted": 1,
            "idempotency_key": "container-smoke-receipt",
        },
        expected=201,
    )
    assert request(prefix + "/snapshot")["order"][0]["state"] == "completed"
    assert request(prefix + "/dispatch", {})["delivered"] == 1
    assert request(prefix + "/dispatch", {})["delivered"] == 0
    return {
        "passed": True,
        "workspace_id": wid,
        "order_id": order["id"],
        "checks": [
            "frontend",
            "auth",
            "migration",
            "constraint_plan",
            "approval_required",
            "idempotent_order",
            "inventory_once",
            "receipt_completed",
            "outbox_once",
        ],
        "model_calls": 0,
        "supplier": "simulated",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18096")
    parser.add_argument("--output", default="evals/results/container-smoke.json")
    args = parser.parse_args()
    result = verify(args.url.rstrip("/"))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
