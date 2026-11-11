"""确定性约束求解。模型可以建议候选，不能裁决预算、库存和交期。"""

from datetime import datetime, timezone
from ortools.sat.python import cp_model
from .knowledge import known_compatible, selected_evidence


def matches_need(product, need):
    """校验单个产品的类别、内存和接口要求。"""
    return (
        product["category"] == need["category"]
        and product.get("memory_gb", 0) >= need["min_memory_gb"]
        and need["interface"] in product.get("interfaces", [])
    )


def solve(requirement, quotes, products, relations=(), fixed_allocations=None):
    """CP-SAT 同时分配所有需求，防止相同报价被多类需求重复占用。

    报价采用含税含运单价的模拟口径。未知接口一律不匹配，证据来源
    随每个方案行返回。五秒内未证明可行时返回明确状态。
    """
    model = cp_model.CpModel()
    variables, valid = {}, []
    current = datetime.now(timezone.utc)
    for q in quotes:
        if (
            datetime.fromisoformat(q["expires_at"]) > current
            and q["delivery_days"] <= requirement["delivery_days"]
        ):
            valid.append(q)
    for i, need in enumerate(requirement["needs"]):
        for j, quote in enumerate(valid):
            if matches_need(products[quote["product_id"]], need):
                variables[i, j] = model.new_int_var(
                    0, min(need["quantity"], quote["stock"]), f"n_{i}_{j}"
                )
        model.add(
            sum(v for (a, _), v in variables.items() if a == i) == need["quantity"]
        )
    for j, quote in enumerate(valid):
        model.add(sum(v for (_, b), v in variables.items() if b == j) <= quote["stock"])
    # 增量重规划只固定已验证未受影响的需求组；其余变量重新求解。
    if fixed_allocations is not None:
        for i, allocation in fixed_allocations.items():
            for j, quote in enumerate(valid):
                if (i, j) in variables:
                    model.add(variables[i, j] == allocation.get(quote["id"], 0))
            if any(qid not in {q["id"] for q in valid} for qid in allocation):
                model.add(False)
    selected = {}
    for pid in {q["product_id"] for q in valid}:
        flag = model.new_bool_var("product_" + pid)
        allocated = sum(
            v for (_, j), v in variables.items() if valid[j]["product_id"] == pid
        )
        model.add(allocated >= 1).only_enforce_if(flag)
        model.add(allocated == 0).only_enforce_if(flag.Not())
        selected[pid] = flag
    from itertools import combinations

    for a, b in combinations(selected, 2):
        if products[a]["category"] != products[b]["category"] and not known_compatible(
            a, b, relations
        ):
            model.add(selected[a] + selected[b] <= 1)
    total = sum(valid[j]["price_cents"] * v for (_, j), v in variables.items())
    model.add(total <= requirement["budget_cents"])
    if not requirement["allow_split"]:
        suppliers = {
            q["supplier"]: model.new_bool_var(f"s_{n}") for n, q in enumerate(valid)
        }
        model.add(sum(suppliers.values()) <= 1)
        for (_, j), v in variables.items():
            model.add(v <= valid[j]["stock"] * suppliers[valid[j]["supplier"]])
    model.minimize(total)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    solver.parameters.num_search_workers = 1
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "status": "infeasible"
            if status == cp_model.INFEASIBLE
            else "budget_exhausted",
            "reason": "预算、库存、交期、参数或供应商限制下未找到可行解",
            "lines": [],
        }
    lines = []
    for (i, j), variable in variables.items():
        count = solver.value(variable)
        if count:
            q = valid[j]
            lines.append(
                {
                    "need_index": i,
                    "purpose": requirement["needs"][i].get("purpose", ""),
                    "quote_id": q["id"],
                    "product_id": q["product_id"],
                    "name": products[q["product_id"]]["name"],
                    "supplier": q["supplier"],
                    "quantity": count,
                    "price_cents": q["price_cents"],
                    "source": q["source"],
                    "delivery_days": q["delivery_days"],
                }
            )
    return {
        "status": "feasible",
        "optimal": status == cp_model.OPTIMAL,
        "total_cents": sum(x["quantity"] * x["price_cents"] for x in lines),
        "lines": lines,
        "compatibility_evidence": selected_evidence(
            [x["product_id"] for x in lines], relations
        ),
    }
