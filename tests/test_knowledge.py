"""相同接口但缺乏人工核验关系时不能默认组合兼容。"""

from .test_procurement import requirement
from procurepilot.planner import solve
from procurepilot.service import CATALOG
from procurepilot.knowledge import COMPATIBILITY


def test_missing_graph_evidence_prevents_combination(env):
    c, _, w = env
    requirement(
        c,
        w,
        needs=[
            {
                "category": "computer",
                "quantity": 1,
                "min_memory_gb": 32,
                "interface": "usb-c",
            },
            {"category": "dock", "quantity": 1, "interface": "usb-c"},
        ],
    )
    snapshot = c.get(f"/api/w/{w}/snapshot").json()
    req = snapshot["requirement"][-1]
    assert solve(req, snapshot["quote"], CATALOG, [])["status"] == "infeasible"
    result = solve(req, snapshot["quote"], CATALOG, COMPATIBILITY)
    assert result["status"] == "feasible" and result["compatibility_evidence"]
