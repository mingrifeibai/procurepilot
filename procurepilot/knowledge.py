"""人工确认的兼容知识。关系必须有来源与状态，接口相同不代表兼容。

本地演示快照用于可复现测试，不能作为真实硬件购买依据。部署图数据库
时也必须遵守同一契约：未知和冲突关系不得进入求解器的可行域。
"""

from itertools import combinations


COMPATIBILITY = [
    {
        "products": [a, b],
        "state": "verified",
        "source": f"fixture://compatibility/{a}/{b}",
    }
    for a, b in [
        ("dev-32", "screen-27"),
        ("dev-32", "dock-c"),
        ("design-64", "screen-27"),
        ("design-64", "dock-c"),
        ("screen-27", "dock-c"),
    ]
]


def known_compatible(a, b, relations):
    if a == b:
        return True
    matching = [
        r for r in relations if set(r["products"]) == {a, b} and r.get("active", True)
    ]
    return bool(matching) and all(
        r["state"] == "verified" and r.get("source") for r in matching
    )


def selected_evidence(product_ids, relations):
    pairs = {frozenset(p) for p in combinations(set(product_ids), 2)}
    return [r for r in relations if frozenset(r["products"]) in pairs]
