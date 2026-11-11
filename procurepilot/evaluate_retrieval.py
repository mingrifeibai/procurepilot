"""固定语料对照原文包含搜索与分段检索，输出逐查询结果和可复核指标。"""

import argparse
import hashlib
import json
import platform
import time
from datetime import datetime, timezone
from pathlib import Path
from .retrieval import search, METHOD


def evaluate(dataset):
    artifacts = dataset["artifacts"]
    reports = {}
    for method in ("literal_substring_baseline", METHOD):
        rows = []
        for query in dataset["queries"]:
            started = time.perf_counter()
            if method == METHOD:
                hits = search(artifacts, query["query"], 20)
                ids = list(dict.fromkeys(h["artifact_id"] for h in hits))[:3]
            else:
                ids = [
                    a["id"]
                    for a in artifacts
                    if query["query"].casefold() in a["text"].casefold()
                ][:3]
            relevant = set(query["relevant"])
            rows.append(
                {
                    "id": query["id"],
                    "query": query["query"],
                    "expected": query["relevant"],
                    "actual_top3": ids,
                    "recall_at_3": len(set(ids) & relevant) / len(relevant)
                    if relevant
                    else None,
                    "reciprocal_rank_at_3": next(
                        (
                            1 / rank
                            for rank, aid in enumerate(ids, 1)
                            if aid in relevant
                        ),
                        0,
                    )
                    if relevant
                    else None,
                    "negative_correct": not ids if not relevant else None,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
        positives = [r for r in rows if r["recall_at_3"] is not None]
        reports[method] = {
            "positive_queries": len(positives),
            "mean_recall_at_3": sum(r["recall_at_3"] for r in positives)
            / len(positives),
            "mean_reciprocal_rank_at_3": sum(
                r["reciprocal_rank_at_3"] for r in positives
            )
            / len(positives),
            "negative_queries": sum(r["negative_correct"] is not None for r in rows),
            "correct_negatives": sum(r["negative_correct"] is True for r in rows),
            "rows": rows,
        }
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="evals/retrieval.json")
    parser.add_argument("--output", default="evals/results/retrieval.json")
    args = parser.parse_args()
    raw = Path(args.dataset).read_bytes()
    report = {
        "suite": "synthetic_lexical_retrieval_comparison",
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "model_calls": 0,
        "limitations": [
            "仅固定合成词法检索回归",
            "没有向量模型、外部索引或人工大规模相关性标注",
            "耗时不代表生产容量",
        ],
        "methods": evaluate(json.loads(raw)),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for method, result in report["methods"].items():
        print(
            f"{method}: Recall@3={result['mean_recall_at_3']:.3f}; MRR@3={result['mean_reciprocal_rank_at_3']:.3f}"
        )
    print(f"Report: {output}")


if __name__ == "__main__":
    main()
