"""以同一组长合成附件比较整段和聚焦上下文，保留真实用量和引用。"""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import time

import httpx

from evaluate_model_modes import ROOT, submit


FACTS = {
    "power-acceptance": (
        r"绝缘(?:等级|级别)?(?:为|达到|须通过)?(?:iii|ⅲ)",
        "绝缘等级 III",
    ),
    "dock-warranty-current": (r"(?:质保|保修)(?:期)?(?:为)?36个?月", "质保 36 个月"),
}


def run(client):
    dataset = json.loads(
        (ROOT / "evals/document_evidence.json").read_text(encoding="utf-8")
    )
    wid = (
        client.post("/api/workspaces", json={"name": "长附件真实模型对照"})
        .raise_for_status()
        .json()["id"]
    )
    prefix = f"/api/w/{wid}"
    ids = {}
    for artifact in dataset["artifacts"]:
        # 大段无关内容放在证据之前，检索后两种策略使用同一批候选。
        padding = "内部归档，无采购结论。"
        prefix_text = (padding * (artifact["padding"] // len(padding) + 1))[
            : artifact["padding"]
        ]
        content = (prefix_text + artifact["text"]).encode()
        row = (
            client.post(
                prefix + "/attachments",
                json={
                    "filename": artifact["id"] + ".txt",
                    "content_base64": base64.b64encode(content).decode(),
                },
            )
            .raise_for_status()
            .json()
        )
        ids[artifact["id"]] = row["id"]
    query = "BC-73 电源板绝缘等级 DX-18 扩展坞现行质保"
    client.post(
        prefix + "/requirements",
        json={
            "title": "合成验收。只根据附件，在摘要逐字引用 BC-73 电源板的绝缘等级和 DX-18 扩展坞现行质保月数，并引用对应附件证据。旧版条款已废止。",
            "budget": "8000",
            "delivery_days": 14,
            "needs": [{"category": "dock", "quantity": 1, "purpose": query}],
        },
    ).raise_for_status()
    search = client.get(prefix + "/evidence/search", params={"query": query})
    if search.status_code == 409:
        job = client.post(prefix + "/evidence/index", json={}).raise_for_status().json()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            indexes = client.get(prefix + "/evidence/index").raise_for_status().json()
            current = next(item for item in indexes if item["id"] == job["id"])
            if current["state"] == "ready" and not current["stale"]:
                break
            if current["state"] in ("failed", "stale"):
                raise RuntimeError("合成附件索引未完成")
            time.sleep(1)
        else:
            raise TimeoutError("等待合成附件索引超时")
    else:
        search.raise_for_status()

    for strategy in ("full_chunk", "focused"):
        result = submit(
            client, prefix, "single", 8000, "preloaded", context_excerpt=strategy
        )
        summary = re.sub(
            r"\s+", "", " ".join(a["summary"] for a in result["assessments"])
        ).casefold()
        evidence_ids = {
            key for item in result["assessments"] for key in item["evidence_ids"]
        }
        checks = []
        for name, (pattern, expected_quote) in FACTS.items():
            artifact_id = ids[name]
            cited = []
            for key in sorted(evidence_ids):
                source, sep, offset = key.rpartition(":offset:")
                span = result["evidence_ranges"].get(key)
                if (
                    sep
                    and source == f"artifact:{artifact_id}"
                    and offset.isdecimal()
                    and span
                ):
                    original = (
                        client.get(
                            f"{prefix}/attachments/{artifact_id}",
                            params={
                                "offset": int(offset),
                                "limit": span["end_offset"] - int(offset),
                            },
                        )
                        .raise_for_status()
                        .json()["text"]
                    )
                    if expected_quote in original:
                        cited.append(key)
            checks.append(
                {
                    "artifact": name,
                    "correct": bool(re.search(pattern, summary)),
                    "supporting_citations": cited,
                }
            )
        yield {
            "workspace_id": wid,
            "context_excerpt": strategy,
            "excerpt_bytes": sum(
                span["bytes"] for span in result["evidence_ranges"].values()
            ),
            "facts_with_correct_answer_and_citation": sum(
                row["correct"] and bool(row["supporting_citations"]) for row in checks
            ),
            "checks": checks,
            **result,
        }
        if result["unknown_usage"] or not result["ledger_matches"] or result["orders"]:
            raise RuntimeError("用量未确认或有异常采购单，停止对照")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8092")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    token = os.getenv("PROCUREPILOT_EVAL_TOKEN", "demo-buyer")
    with httpx.Client(
        base_url=args.url,
        headers={"Authorization": "Bearer " + token},
        timeout=15,
        trust_env=False,
    ) as client:
        status = client.get("/api/model-status").raise_for_status().json()
        if not status["configured"]:
            raise RuntimeError("8092 尚未配置模型")
        report = {"model": status["model_name"], "planned_token_cap": 16000, "runs": []}
        for row in run(client):
            report["runs"].append(row)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        full, focused = report["runs"]
        report["passed"] = (
            all(
                row["state"] == "completed"
                and row["calls"] > 0
                and row["facts_with_correct_answer_and_citation"] == len(FACTS)
                for row in (full, focused)
            )
            and 0 < focused["prompt_tokens"] < full["prompt_tokens"]
            and 0 < focused["excerpt_bytes"] < full["excerpt_bytes"]
        )
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if not report["passed"]:
            raise SystemExit("真实对照未通过，请查看事实、引用与 Token 记录")
            print(
                row["context_excerpt"],
                row["state"],
                row["prompt_tokens"],
                row["facts_with_correct_answer_and_citation"],
            )


if __name__ == "__main__":
    main()
