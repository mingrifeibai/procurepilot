"""依赖感知的增量重规划：固定未受影响需求组，全局约束冲突时解除固定。"""

from collections import Counter
from datetime import datetime, timezone
from .planner import solve


def replan(
    requirement, previous_requirement, previous_plan, quotes, products, relations
):
    """需求组按索引和完整输入比较；重排会被保守视为变更，不错误复用。

    兼容、预算、拆单是全局约束，固定组也必须通过完整求解校验。
    固定导致无解时回退全量求解，避免把局部最优误报为业务无解。
    """
    byid = {q["id"]: q for q in quotes}
    fixed = {}
    affected = []
    now = datetime.now(timezone.utc)
    old_needs = previous_requirement["needs"]
    for i, need in enumerate(requirement["needs"]):
        lines = [l for l in previous_plan.get("lines", []) if l["need_index"] == i]
        stable = i < len(old_needs) and old_needs[i] == need and bool(lines)
        for line in lines:
            q = byid.get(line["quote_id"])
            snapshot = previous_plan["quote_snapshots"].get(line["quote_id"])
            stable = stable and q is not None and snapshot is not None
            if stable:
                stable = (
                    all(
                        q[k] == snapshot[k]
                        for k in (
                            "price_cents",
                            "stock",
                            "delivery_days",
                            "product_id",
                            "supplier",
                            "expires_at",
                        )
                    )
                    and datetime.fromisoformat(q["expires_at"]) > now
                )
        if stable:
            allocation = Counter()
            for l in lines:
                allocation[l["quote_id"]] += l["quantity"]
            fixed[i] = dict(allocation)
        else:
            affected.append(i)
    result = solve(requirement, quotes, products, relations, fixed_allocations=fixed)
    fallback = result["status"] != "feasible" and bool(fixed)
    if fallback:
        result = solve(requirement, quotes, products, relations)
    old_counts = Counter()
    new_counts = Counter()
    for l in previous_plan.get("lines", []):
        old_counts[l["need_index"], l["quote_id"]] += l["quantity"]
    for l in result.get("lines", []):
        new_counts[l["need_index"], l["quote_id"]] += l["quantity"]
    changes = [
        {
            "need_index": i,
            "quote_id": qid,
            "before": old_counts[i, qid],
            "after": new_counts[i, qid],
        }
        for i, qid in sorted(old_counts.keys() | new_counts.keys())
        if old_counts[i, qid] != new_counts[i, qid]
    ]
    result["replanning"] = {
        "strategy": "incremental",
        "affected_need_indices": affected,
        "retained_need_indices": [] if fallback else list(fixed),
        "full_fallback": fallback,
        "changes": changes,
        "approval_required": True,
    }
    return result
