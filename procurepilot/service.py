"""采购用例层：版本、审批、幂等执行、履约及可恢复运行。

所有入口必须由接口层先执行工作区授权；这里再次通过 workspace_id
定位资源。执行时重新核验数据库中的权威状态，不信任模型或前端缓存。
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from fastapi import HTTPException
from .domain import cents
from .storage import Workspace, add, records, view
from .planner import solve
from .graph import relations as graph_relations, fingerprint as graph_fingerprint
from .graph import matches_plan_snapshot


PRODUCTS = [
    {
        "id": "dev-32",
        "name": "研发工作站 32G",
        "category": "computer",
        "memory_gb": 32,
        "interfaces": ["usb-c", "hdmi"],
        "source": "fixture://catalog/dev-32",
    },
    {
        "id": "design-64",
        "name": "设计工作站 64G",
        "category": "computer",
        "memory_gb": 64,
        "interfaces": ["usb-c", "hdmi"],
        "source": "fixture://catalog/design-64",
    },
    {
        "id": "screen-27",
        "name": "27 英寸 4K 显示器",
        "category": "monitor",
        "memory_gb": 0,
        "interfaces": ["usb-c", "hdmi"],
        "source": "fixture://catalog/screen-27",
    },
    {
        "id": "dock-c",
        "name": "USB-C 扩展坞",
        "category": "dock",
        "memory_gb": 0,
        "interfaces": ["usb-c"],
        "source": "fixture://catalog/dock-c",
    },
]
CATALOG = {p["id"]: p for p in PRODUCTS}


def fail(message, code=409):
    raise HTTPException(code, message)


def get(session, wid, kind, rid):
    for item in records(session, wid, kind):
        if item.id == rid:
            return item
    fail("资源不存在或不属于当前工作区", 404)


def latest(session, wid, kind):
    items = records(session, wid, kind)
    if not items:
        fail(f"请先创建 {kind}", 422)
    return items[-1]


def audit(session, wid, actor, action, resource):
    add(session, wid, "audit", {"actor": actor, "action": action, "resource": resource})


def create_workspace(session, name, actor, organization_id=None):
    wid = str(uuid4())
    session.add(Workspace(id=wid, name=name, organization_id=organization_id))
    session.flush()
    for product, price in zip(PRODUCTS, [580000, 960000, 180000, 45000]):
        add(
            session,
            wid,
            "quote",
            {
                "product_id": product["id"],
                "supplier": "演示供应商 A",
                "price_cents": price,
                "stock": 100,
                "delivery_days": 7,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(hours=72)
                ).isoformat(),
                "source": product["source"],
                "simulated": True,
            },
        )
    audit(session, wid, actor, "workspace.created", wid)
    return workspace_view(session.get(Workspace, wid))


def workspace_view(workspace):
    result = {"id": workspace.id, "name": workspace.name}
    if workspace.organization_id is not None:
        result["organization_id"] = workspace.organization_id
    return result


def save_requirement(session, wid, requirement, actor):
    data = requirement.model_dump(mode="json")
    data["budget_cents"] = cents(data.pop("budget"))
    data["version"] = len(records(session, wid, "requirement")) + 1
    item = add(session, wid, "requirement", data)
    audit(session, wid, actor, "requirement.versioned", item.id)
    return view(item)


def save_quote(session, wid, quote, actor):
    if quote.product_id not in CATALOG:
        fail("未知产品", 422)
    data = quote.model_dump(mode="json")
    data["price_cents"] = cents(data.pop("unit_price"))
    data["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(hours=data.pop("valid_hours"))
    ).isoformat()
    data["simulated"] = True
    item = add(session, wid, "quote", data)
    audit(session, wid, actor, "quote.created", item.id)
    return view(item)


def plan(session, wid, actor, previous_plan_id=None):
    relations = graph_relations()
    req = latest(session, wid, "requirement")
    quotes = [view(q) for q in records(session, wid, "quote")]
    if previous_plan_id:
        from .replanning import replan

        reference = get(session, wid, "plan", previous_plan_id)
        old_req = get(session, wid, "requirement", reference.data["requirement_id"])
        result = replan(
            req.data, old_req.data, reference.data, quotes, CATALOG, relations
        )
    else:
        result = solve(req.data, quotes, CATALOG, relations)
    previous = records(session, wid, "plan")
    old = previous[-1].data if previous else {}
    result.update(
        requirement_id=req.id,
        graph_fingerprint=graph_fingerprint(relations),
        version=len(previous) + 1,
        quote_snapshots={q.id: q.data for q in records(session, wid, "quote")},
        delta_cents=result.get("total_cents", 0) - old.get("total_cents", 0),
        previous_plan_id=previous_plan_id or (previous[-1].id if previous else None),
    )
    item = add(session, wid, "plan", result)
    audit(session, wid, actor, "plan.generated", item.id)
    return view(item)


def validate_plan(session, wid, item):
    data = item.data
    if data["status"] != "feasible":
        fail("不可行方案禁止审批或执行")
    if latest(session, wid, "requirement").id != data["requirement_id"]:
        fail("需求已变更，请重新规划和审批")
    if not matches_plan_snapshot(graph_relations(), data.get("graph_fingerprint")):
        fail("兼容图谱已变化，请重新规划和审批")
    quantities = Counter()
    for line in data["lines"]:
        quantities[line["quote_id"]] += line["quantity"]
    for qid, quantity in quantities.items():
        q = get(session, wid, "quote", qid)
        snapshot = data["quote_snapshots"][qid]
        if datetime.fromisoformat(q.data["expires_at"]) <= datetime.now(timezone.utc):
            fail("报价已过期，请重新规划")
        if q.data["stock"] < quantity:
            fail("库存不足，请重新规划")
        if any(
            q.data[key] != snapshot[key]
            for key in (
                "price_cents",
                "delivery_days",
                "product_id",
                "supplier",
                "expires_at",
            )
        ):
            fail("报价条款已变化，请重新审批")
    return quantities


def approve(session, wid, pid, actor):
    item = get(session, wid, "plan", pid)
    validate_plan(session, wid, item)
    existing = [
        a for a in records(session, wid, "approval") if a.data["plan_id"] == pid
    ]
    if existing:
        return view(existing[0])
    result = add(
        session,
        wid,
        "approval",
        {"plan_id": pid, "actor": actor, "requirement_id": item.data["requirement_id"]},
    )
    audit(session, wid, actor, "plan.approved", pid)
    return view(result)


def execute(session, wid, pid, key, actor):
    for order in records(session, wid, "order"):
        if order.data["idempotency_key"] == key:
            if order.data["plan_id"] != pid:
                fail("幂等键已用于其他方案")
            return view(order)
        if order.data["plan_id"] == pid:
            fail("此方案已经执行，请查询已有采购单")
    item = get(session, wid, "plan", pid)
    counts = validate_plan(session, wid, item)
    if not any(a.data["plan_id"] == pid for a in records(session, wid, "approval")):
        fail("需要审批当前方案版本", 403)
    for qid, count in counts.items():
        quote = get(session, wid, "quote", qid)
        quote.data = {**quote.data, "stock": quote.data["stock"] - count}
    order = add(
        session,
        wid,
        "order",
        {
            "plan_id": pid,
            "idempotency_key": key,
            "state": "placed",
            "lines": item.data["lines"],
            "total_cents": item.data["total_cents"],
            "simulated_supplier": True,
        },
    )
    add(
        session,
        wid,
        "outbox",
        {
            "type": "order.placed",
            "order_id": order.id,
            "state": "pending",
            "attempts": 0,
        },
    )
    audit(session, wid, actor, "order.placed", order.id)
    return view(order)


def receive(session, wid, oid, receipt, actor):
    order = get(session, wid, "order", oid)
    if order.data["state"] == "cancelled":
        fail("已取消的订单不能登记到货")
    payload = receipt.model_dump()
    previous = records(session, wid, "receipt")
    for r in previous:
        if r.data["idempotency_key"] == receipt.idempotency_key:
            if any(
                r.data.get("original_payload", r.data).get(k) != v
                for k, v in {**payload, "order_id": oid}.items()
            ):
                fail("幂等键对应的到货内容不同")
            return view(r)
    if receipt.accepted > receipt.quantity:
        fail("验收数量不得超过到货数量", 422)
    expected = sum(
        l["quantity"] for l in order.data["lines"] if l["quote_id"] == receipt.quote_id
    )
    arrived = sum(
        r.data["quantity"]
        for r in previous
        if r.data["order_id"] == oid and r.data["quote_id"] == receipt.quote_id
    )
    if not expected or arrived + receipt.quantity > expected:
        fail("到货产品不在订单内或累计到货超出采购数量", 422)
    item = add(
        session,
        wid,
        "receipt",
        {**payload, "order_id": oid, "original_payload": {**payload, "order_id": oid}},
    )
    all_receipts = [
        r for r in records(session, wid, "receipt") if r.data["order_id"] == oid
    ]
    received = sum(r.data["quantity"] for r in all_receipts)
    accepted = sum(r.data["accepted"] for r in all_receipts)
    quantity = sum(l["quantity"] for l in order.data["lines"])
    state = (
        "exception"
        if accepted < received
        else "completed"
        if accepted == quantity
        else "partial"
    )
    order.data = {**order.data, "state": state}
    if receipt.accepted < receipt.quantity:
        add(
            session,
            wid,
            "exception",
            {
                "order_id": oid,
                "receipt_id": item.id,
                "state": "open",
                "note": receipt.note,
            },
        )
    audit(session, wid, actor, "receipt.registered", item.id)
    return view(item)


def dispatch(session, wid):
    """本地模拟履约投递器。消费标记和通知落库原子提交，重复调用无副作用。"""
    delivered = 0
    for event in records(session, wid, "outbox"):
        if event.data["state"] == "pending":
            add(
                session,
                wid,
                "notification",
                {
                    "event_id": event.id,
                    "order_id": event.data["order_id"],
                    "message": "模拟供应商已接单",
                },
            )
            event.data = {
                **event.data,
                "state": "delivered",
                "attempts": event.data["attempts"] + 1,
            }
            delivered += 1
    return {"delivered": delivered}


def cancel(session, wid, oid, actor):
    """本地模拟订单取消。已有到货时禁止直接回补库存，避免账实不符。"""
    order = get(session, wid, "order", oid)
    if order.data["state"] == "cancelled":
        return view(order)
    if any(r.data["order_id"] == oid for r in records(session, wid, "receipt")):
        fail("订单已有到货记录，需要走退货流程，不能直接取消")
    for line in order.data["lines"]:
        quote = get(session, wid, "quote", line["quote_id"])
        quote.data = {**quote.data, "stock": quote.data["stock"] + line["quantity"]}
    order.data = {**order.data, "state": "cancelled"}
    for event in records(session, wid, "outbox"):
        if event.data.get("order_id") == oid and event.data["state"] == "pending":
            event.data = {**event.data, "state": "cancelled"}
    audit(session, wid, actor, "order.cancelled", oid)
    return view(order)


def resolve(session, wid, eid, body, actor):
    """复检纠正验收，原始记录另存修订凭证，避免无痕覆盖。"""
    issue = get(session, wid, "exception", eid)
    if issue.data["state"] == "resolved":
        fail("此异常已处理")
    receipt = get(session, wid, "receipt", issue.data["receipt_id"])
    accepted = body.accepted_after_reinspection
    if accepted != receipt.data["quantity"]:
        fail("复检关闭仅允许全数验收合格；退换货异常应保持待处理", 422)
    add(
        session,
        wid,
        "receipt_revision",
        {
            "receipt_id": receipt.id,
            "previous": receipt.data,
            "actor": actor,
            "note": body.note,
        },
    )
    receipt.data = {**receipt.data, "accepted": accepted}
    issue.data = {
        **issue.data,
        "state": "resolved",
        "resolution": body.note,
        "actor": actor,
    }
    order = get(session, wid, "order", issue.data["order_id"])
    received = [
        r for r in records(session, wid, "receipt") if r.data["order_id"] == order.id
    ]
    count = sum(r.data["quantity"] for r in received)
    passed = sum(r.data["accepted"] for r in received)
    total = sum(l["quantity"] for l in order.data["lines"])
    state = (
        "exception" if passed < count else "completed" if passed == total else "partial"
    )
    order.data = {**order.data, "state": state}
    audit(session, wid, actor, "exception.resolved", eid)
    return view(issue)
