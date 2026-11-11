"""固定合成采购任务对照。用量与时间可比较，内容质量仍需人工判读。"""

import argparse
import hashlib
import json
import math
import os
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


def score_tool_evidence(run, gold_pairs):
    """按人工标注的兼容组合评分。引用覆盖只统计 ID，语义支持率另行审阅。"""
    results = {
        tuple(sorted(result["arguments"].values())): result["output"]
        for result in run["tool_results"]
        if result["tool"] == "check_compatibility" and result["state"] == "completed"
    }
    verifier_ids = {
        item
        for assessment in run["assessments"]
        if assessment["role"] == "证据核验员"
        for item in assessment["evidence_ids"]
    }
    checks = []
    for gold in gold_pairs:
        pair = tuple(sorted(gold["products"]))
        output = results.get(pair, {})
        supported = {
            item["id"]
            for item in output.get("evidence", [])
            if item["source"] == gold["source"]
        }
        checks.append(
            {
                "products": list(pair),
                "expected_state": gold["state"],
                "actual_state": output.get("state"),
                "source_match": bool(supported),
                "cited_by_verifier": bool(supported & verifier_ids),
            }
        )
    return checks


def submit(
    client,
    prefix,
    mode,
    max_tokens,
    catalog_mode,
    resume_once=False,
    context_excerpt="focused",
):
    started = time.monotonic()
    job = (
        client.post(
            prefix + "/analysis-jobs",
            json={
                "idempotency_key": "model-eval-" + uuid4().hex,
                "coordination_mode": mode,
                "catalog_mode": catalog_mode,
                "max_tokens": max_tokens,
                "context_excerpt": context_excerpt,
            },
        )
        .raise_for_status()
        .json()
    )
    analysis_id = job["id"]
    deadline = started + 330

    def read_result(current):
        while (
            current["dispatch_state"] in ("queued", "leased")
            and time.monotonic() < deadline
        ):
            time.sleep(1)
            current = (
                client.get(f"{prefix}/analyses/{analysis_id}").raise_for_status().json()
            )
        snapshot = client.get(prefix + "/snapshot").raise_for_status().json()
        calls = [c for c in snapshot["model_call"] if c["analysis_id"] == analysis_id]
        return current, snapshot, calls

    job, snapshot, calls = read_result(job)
    recovery = None
    if (
        resume_once
        and job["state"] == "waiting_response"
        and job["dispatch_state"] == "done"
        and time.monotonic() < deadline
        and calls
        and all(
            c.get("usage") is not None and c["state"] not in ("pending", "uncertain")
            for c in calls
        )
        and sum(sum((c.get("usage") or {}).values()) for c in calls) == job["tokens"]
        and not snapshot["order"]
    ):
        recovery = {"tokens_before": job["tokens"], "error_before": job["error"]}
        tool_results = job.get("tool_results", [])
        queued = (
            client.post(f"{prefix}/analysis-jobs/{analysis_id}/resume")
            .raise_for_status()
            .json()
        )
        job, snapshot, calls = read_result(queued)
        recovery["mcp_results_reused"] = tool_results == job.get("tool_results", [])
    usage = sum(sum((call.get("usage") or {}).values()) for call in calls)
    return {
        "mode": mode,
        "catalog_mode": job.get("catalog_mode", "preloaded"),
        "evidence_gap": job.get("evidence_gap"),
        "analysis_id": job["id"],
        "state": job["state"],
        "dispatch_state": job["dispatch_state"],
        "error": job.get("error"),
        "roles": job["roles"],
        "delegation_state": job.get("delegation_state"),
        "policy_added_roles": job.get("policy_added_roles", []),
        "tokens": job["tokens"],
        "prompt_tokens": sum(
            (c.get("usage") or {}).get("prompt_tokens", 0) for c in calls
        ),
        "completion_tokens": sum(
            (c.get("usage") or {}).get("completion_tokens", 0) for c in calls
        ),
        "cost": job.get("cost"),
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "ledger_matches": usage == job["tokens"],
        "calls": len(calls),
        "unknown_usage": bool(job.get("uncertain_usage"))
        or any(c["state"] in ("pending", "uncertain") for c in calls),
        "assessments": job["assessments"],
        "tool_results": job.get("tool_results", []),
        "parallel_batches": job.get("parallel_batches", []),
        "orders": len(snapshot["order"]),
        "evidence_ranges": job.get("evidence_ranges", {}),
        "response_recovery": recovery,
    }


def summarize(runs):
    """按策略记录用量和延迟。P95 使用最近秩，样本数随报告保存。"""
    groups = {}
    for row in runs:
        groups.setdefault((row["mode"], row["catalog_mode"]), []).append(row)
    summary = []
    for (mode, catalog), rows in groups.items():
        latencies = sorted(row["elapsed_ms"] for row in rows)
        summary.append(
            {
                "mode": mode,
                "catalog_mode": catalog,
                "runs": len(rows),
                "states": dict(Counter(row["state"] for row in rows)),
                "known_tokens": sum(row["tokens"] for row in rows),
                "unknown_usage_runs": sum(row["unknown_usage"] for row in rows),
                "elapsed_ms_p50": latencies[math.ceil(len(rows) * 0.50) - 1],
                "elapsed_ms_p95": latencies[math.ceil(len(rows) * 0.95) - 1],
            }
        )
    return summary


def main():
    raw = (ROOT / "evals/model_cases.json").read_bytes()
    dataset = json.loads(raw)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", action="append", choices=[case["id"] for case in dataset]
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=["single", "fixed", "dynamic"],
        default=["single", "dynamic"],
    )
    parser.add_argument(
        "--tokens-per-run", type=int, choices=[8000, 16000, 24000], default=16000
    )
    parser.add_argument("--repeat", type=int, choices=[1, 2, 3], default=1)
    parser.add_argument(
        "--catalog-mode", choices=["preloaded", "on_demand"], default="preloaded"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--plan-only", action="store_true", help="仅生成矩阵和预算上限，不提交模型任务"
    )
    parser.add_argument(
        "--prepare-plan",
        action="store_true",
        help="模型分析前先生成并确认可行的求解器方案",
    )
    parser.add_argument(
        "--require-completion",
        action="store_true",
        help="任务未完成或验收标注未通过时返回非零退出码",
    )
    parser.add_argument(
        "--resume-on-response-error",
        action="store_true",
        help="用量确定且账本一致时，在原任务预算内恢复一次格式或金额校验失败的任务",
    )
    args = parser.parse_args()
    if args.case:
        cases = [case for case in dataset if case["id"] in args.case]
    else:
        cases = [case for case in dataset if "expected_plan_cents" in case]
    modes = list(dict.fromkeys(args.modes))
    if args.catalog_mode == "on_demand" and modes != ["dynamic"]:
        parser.error("按需目录评测需要 --modes dynamic")
    report = {
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "planned_tokens_cap": len(cases)
        * len(modes)
        * args.repeat
        * args.tokens_per_run,
        "matrix": {
            "cases": [case["id"] for case in cases],
            "modes": modes,
            "catalog_mode": args.catalog_mode,
            "repeat": args.repeat,
            "tokens_per_run": args.tokens_per_run,
            "prepare_plan": args.prepare_plan,
            "require_completion": args.require_completion,
            "resume_on_response_error": args.resume_on_response_error,
        },
        "runs": [],
        "summary": [],
    }
    if args.plan_only:
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return
    token = os.getenv("PROCUREPILOT_EVAL_TOKEN", "demo-buyer")
    with httpx.Client(
        base_url="http://127.0.0.1:8092",
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        timeout=15,
    ) as client:
        status = client.get("/api/model-status").raise_for_status().json()
        assert status["configured"], "模型服务尚未配置"
        report["model"] = status["model_name"]
        for case in cases:
            wid = (
                client.post(
                    "/api/workspaces", json={"name": "模型对照 · " + case["id"]}
                )
                .raise_for_status()
                .json()["id"]
            )
            prefix = f"/api/w/{wid}"
            client.post(
                prefix + "/requirements", json=case["requirement"]
            ).raise_for_status()
            for quote in case.get("quotes", []):
                client.post(prefix + "/quotes", json=quote).raise_for_status()
            plan = None
            expected_total = case.get(
                "expected_plan_cents", case.get("expected_prepared_plan_cents")
            )
            if args.prepare_plan or expected_total is not None:
                plan = client.post(prefix + "/plans").raise_for_status().json()
                assert plan["status"] == "feasible", (case["id"], plan["status"])
                if expected_total is not None:
                    assert plan["total_cents"] == expected_total
            for repeat in range(args.repeat):
                for mode in modes:
                    row = submit(
                        client,
                        prefix,
                        mode,
                        args.tokens_per_run,
                        args.catalog_mode,
                        resume_once=args.resume_on_response_error,
                    )
                    row.update(
                        case=case["id"],
                        workspace_id=wid,
                        repeat=repeat + 1,
                        plan_id=plan["id"] if plan else None,
                        plan_total_cents=plan["total_cents"] if plan else None,
                    )
                    if case.get("gold_graph_pairs"):
                        row["graph_evidence_checks"] = score_tool_evidence(
                            row, case["gold_graph_pairs"]
                        )
                    if case.get("gold_catalog_id"):
                        expected = case["gold_catalog_id"]
                        row["catalog_evidence_check"] = {
                            "expected": expected,
                            "found": any(
                                evidence["id"] == expected
                                for result in row["tool_results"]
                                if result["tool"] == "search_products"
                                for evidence in result.get("output", {}).get(
                                    "evidence", []
                                )
                            ),
                        }
                    if case.get("gold_catalog_ids"):
                        actual_catalog = {
                            evidence["id"]
                            for result in row["tool_results"]
                            if result["tool"] == "search_products"
                            for evidence in result.get("output", {}).get("evidence", [])
                        }
                        verified = {
                            evidence_id
                            for assessment in row["assessments"]
                            if assessment["role"] == "证据核验员"
                            for evidence_id in assessment["evidence_ids"]
                        }
                        row["catalog_evidence_checks"] = [
                            {
                                "id": expected,
                                "found": expected in actual_catalog,
                                "cited_by_verifier": expected in verified,
                            }
                            for expected in case["gold_catalog_ids"]
                        ]
                    if case.get("gold_parallel_roles"):
                        expected_roles = set(case["gold_parallel_roles"])
                        row["parallel_role_check"] = any(
                            expected_roles <= set(batch["roles"])
                            for batch in row["parallel_batches"]
                        )
                    if case.get("gold_quantity_checks") and mode == "dynamic":
                        quantities = next(
                            (
                                item.get("quantity_checks") or []
                                for item in row["assessments"]
                                if item["role"] == "证据核验员"
                            ),
                            [],
                        )
                        row["quantity_check_matches"] = (
                            sorted(quantities, key=lambda item: item["need_index"])
                            == case["gold_quantity_checks"]
                        )
                        row["acceptance_matches"] = (
                            row["quantity_check_matches"]
                            and row.get("parallel_role_check", True)
                            and all(
                                item["found"] and item["cited_by_verifier"]
                                for item in row.get("catalog_evidence_checks", [])
                            )
                        )
                    report["runs"].append(row)
                    report["summary"] = summarize(report["runs"])
                    args.output.write_text(
                        json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print(
                        f"{row['case']} {mode} {row['state']} {row['tokens']} tokens",
                        flush=True,
                    )
                    if (
                        row["unknown_usage"]
                        or not row["ledger_matches"]
                        or row["orders"]
                        or row["dispatch_state"] in ("queued", "leased")
                    ):
                        raise SystemExit("运行结果无法安全继续对照，已保存账本")
    if args.require_completion and any(
        row["state"] != "completed"
        or row.get("acceptance_matches") is False
        or any(
            check["actual_state"] != check["expected_state"]
            or not check["source_match"]
            or not check["cited_by_verifier"]
            for check in row.get("graph_evidence_checks", [])
        )
        for row in report["runs"]
    ):
        raise SystemExit("至少一个模型任务未完成或未通过标注检查，已保存验收记录")


if __name__ == "__main__":
    main()
