"""固定采购约束回归集；报告只衡量求解正确性，不冒充模型效果评测。"""

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import statistics
import time
from .domain import Requirement, cents
from .knowledge import COMPATIBILITY, known_compatible
from .planner import solve
from .service import CATALOG


def inputs(task):
    need = {
        "category": "computer",
        "quantity": task.get("quantity", 1),
        "min_memory_gb": task.get("min_memory_gb", 32),
        "interface": task.get("interface", "usb-c"),
    }
    needs = [need]
    if task.get("duplicate"):
        needs.append(deepcopy(need))
    if task.get("bundle"):
        needs.extend(
            {"category": c, "quantity": need["quantity"], "interface": "usb-c"}
            for c in ("monitor", "dock")
        )
    req = Requirement(
        title=task["id"],
        budget=task.get("budget", "180000"),
        delivery_days=task.get("delivery_days", 14),
        allow_split=task.get("allow_split", True),
        needs=needs,
    ).model_dump(mode="json")
    req["budget_cents"] = cents(req.pop("budget"))
    quotes = [
        {
            "id": "q-" + pid,
            "product_id": pid,
            "supplier": CATALOG[pid]["category"]
            if task.get("supplier_per_category")
            else "A",
            "price_cents": price,
            "stock": task.get("stock", 100),
            "delivery_days": 7,
            "expires_at": "2000-01-01T00:00:00+00:00"
            if task.get("expired")
            else "2099-01-01T00:00:00+00:00",
            "source": "fixture://eval/" + pid,
        }
        for pid, price in zip(CATALOG, [580000, 960000, 180000, 45000])
        if not task.get("only_dev") or pid == "dev-32"
    ]
    return req, quotes, COMPATIBILITY if task.get("relations", True) else []


def violations(req, quotes, relations, result):
    """独立核对输出分配，不依赖求解器的约束对象。"""
    if result["status"] != "feasible":
        return []
    errors = []
    counts = Counter()
    stocks = Counter()
    catalog = {q["id"]: q for q in quotes}
    cost = 0
    for line in result["lines"]:
        q = catalog[line["quote_id"]]
        n = line["quantity"]
        need = req["needs"][line["need_index"]]
        p = CATALOG[q["product_id"]]
        counts[line["need_index"]] += n
        stocks[q["id"]] += n
        cost += n * q["price_cents"]
        if (
            n <= 0
            or p["category"] != need["category"]
            or p.get("memory_gb", 0) < need["min_memory_gb"]
            or need["interface"] not in p["interfaces"]
        ):
            errors.append("parameters")
        if q["delivery_days"] > req["delivery_days"]:
            errors.append("delivery")
        if datetime.fromisoformat(q["expires_at"]) <= datetime.now(timezone.utc):
            errors.append("expired")
    if any(counts[i] != n["quantity"] for i, n in enumerate(req["needs"])):
        errors.append("quantity")
    if any(n > catalog[qid]["stock"] for qid, n in stocks.items()):
        errors.append("stock")
    if cost > req["budget_cents"] or cost != result["total_cents"]:
        errors.append("budget_or_arithmetic")
    if not req["allow_split"] and len({l["supplier"] for l in result["lines"]}) > 1:
        errors.append("split")
    products = {l["product_id"] for l in result["lines"]}
    for a in products:
        for b in products:
            if CATALOG[a]["category"] != CATALOG[b][
                "category"
            ] and not known_compatible(a, b, relations):
                errors.append("compatibility")
    return sorted(set(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="evals/tasks.json")
    parser.add_argument("--output", default="evals/results/latest.json")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 100:
        parser.error("repeats must be 1..100")
    raw = Path(args.dataset).read_bytes()
    tasks = json.loads(raw)
    rows = []
    for task in tasks:
        req, quotes, relations = inputs(task)
        for repetition in range(args.repeats):
            started = time.perf_counter()
            result = solve(req, quotes, CATALOG, relations)
            elapsed = (time.perf_counter() - started) * 1000
            errors = violations(req, quotes, relations, result)
            rows.append(
                {
                    "task": task["id"],
                    "repetition": repetition,
                    "expected": task["expected"],
                    "actual": result["status"],
                    "violations": errors,
                    "passed": result["status"] == task["expected"] and not errors,
                    "elapsed_ms": elapsed,
                    "result": result,
                }
            )
    latency = sorted(r["elapsed_ms"] for r in rows)
    report = {
        "suite": "deterministic_constraint_regression",
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "p50_ms": statistics.median(latency),
        "p95_ms": latency[min(len(latency) - 1, int(len(latency) * 0.95))],
        "model_calls": 0,
        "model_tokens": 0,
        "limitations": [
            "模拟报价和兼容快照",
            "仅约束回归；不衡量真实模型完成率",
            "耗时不代表生产压测结果",
        ],
        "rows": rows,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{report['passed']}/{len(rows)} passed; report: {output}")
    if report["passed"] != len(rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
