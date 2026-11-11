"""全量/增量重规划对照：报告改动量、成本和耗时，不预设增量一定更优。"""

from copy import deepcopy
import argparse
import json
from pathlib import Path
import time
from .evaluate import inputs, violations
from .planner import solve
from .replanning import replan
from .service import CATALOG


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="evals/results/replanning-comparison.json")
    args = parser.parse_args()
    req, quotes, relations = inputs({"id": "baseline", "bundle": True, "quantity": 20})
    original = solve(req, quotes, CATALOG, relations)
    original["quote_snapshots"] = {q["id"]: deepcopy(q) for q in quotes}
    scenarios = []
    for name in (
        "team_grows",
        "unrelated_cheaper_quote",
        "budget_requires_unlock",
        "selected_quote_out_of_stock",
    ):
        changed = deepcopy(req)
        offers = deepcopy(quotes)
        changed["needs"][0]["quantity"] = 21
        changed["budget_cents"] = 20000000
        if name in ("unrelated_cheaper_quote", "budget_requires_unlock"):
            cheap = {
                **offers[2],
                "id": "new-screen",
                "supplier": "B",
                "price_cents": 1000,
            }
            offers.append(cheap)
        if name == "budget_requires_unlock":
            changed["budget_cents"] = 14000000
        if name == "selected_quote_out_of_stock":
            offers[0]["stock"] = 0
            offers.append(
                {
                    **quotes[0],
                    "id": "replacement-dev",
                    "supplier": "B",
                    "price_cents": 600000,
                }
            )
        results = {}
        for mode in ("full", "incremental"):
            started = time.perf_counter()
            result = (
                solve(changed, offers, CATALOG, relations)
                if mode == "full"
                else replan(changed, req, original, offers, CATALOG, relations)
            )
            elapsed = (time.perf_counter() - started) * 1000
            errors = violations(changed, offers, relations, result)
            if result["status"] != "feasible" or errors:
                raise AssertionError((name, mode, errors))
            old = {
                (l["need_index"], l["quote_id"]): l["quantity"]
                for l in original["lines"]
            }
            new = {
                (l["need_index"], l["quote_id"]): l["quantity"] for l in result["lines"]
            }
            results[mode] = {
                "elapsed_ms": elapsed,
                "total_cents": result["total_cents"],
                "allocation_change_units": sum(
                    abs(new.get(k, 0) - old.get(k, 0)) for k in old.keys() | new.keys()
                ),
                "result": result,
            }
        scenarios.append({"scenario": name, "results": results})
    report = {
        "suite": "full_vs_incremental",
        "baseline": original,
        "scenarios": scenarios,
        "limitations": [
            "固定模拟快照，不涉及模型调用",
            "增量保留旧选择，可能比全量最低成本更贵",
            "改动单位为分配数量L1距离，不能当作产品更换台数",
            "单次耗时仅供复现，不代表性能结论",
        ],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{len(scenarios)} paired scenarios passed: {output}")


if __name__ == "__main__":
    main()
