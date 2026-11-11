"""Measure local BGE reranking on a fixed, labeled procurement candidate set."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from time import perf_counter


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from procurepilot import reranking, retrieval  # noqa: E402


WEIGHT_SHA256 = "912fc1215c2dbff6499700534bd8d31253af01573861abbfc43afd1fab6cce5d"
# Ask for the facts without giving the target value to either ranking method.
QUESTIONS = {
    "power-late": "BC-73 电源板通过验收需要哪种绝缘级别",
    "warranty-version": "DX-18 扩展坞现行售后保修期限多长",
    "delivery-early": "SK-31 显示器什么时间可以送货",
    "memory-boundary": "MEM-Q9 服务器的内存容量和纠错要求",
    "two-artifacts": "RF-42 温度探头箱内件数和最近的校准时间要求",
    "english-case": "LX-07 what paperwork and spares are needed at handover",
    "source-boundary": "CL-09 通过验收需要多长时间的老化测试",
}


def digest(path):
    hash_ = hashlib.sha256()
    with path.open("rb") as weight:
        for block in iter(lambda: weight.read(1024 * 1024), b""):
            hash_.update(block)
    return hash_.hexdigest()


def grade(hits, gold):
    ranks = [
        next(
            (
                rank
                for rank, hit in enumerate(hits, 1)
                if hit["artifact_id"] == item["artifact_id"]
                and item["quote"] in hit["snippet"]
            ),
            None,
        )
        for item in gold
    ]
    found = [rank for rank in ranks if rank is not None]
    return {
        "top_artifact": hits[0]["artifact_id"] if hits else None,
        "gold_ranks": ranks,
        "reciprocal_rank": 1 / min(found) if found else 0.0,
        "recall_at_3": sum(rank <= 3 for rank in found) / len(ranks),
    }


def run(directory):
    sha = digest(directory / "onnx" / "model_int8.onnx")
    if sha != WEIGHT_SHA256:
        raise ValueError("Unexpected ONNX weight digest")
    os.environ["RERANK_MODEL_DIR"] = str(directory)
    start = perf_counter()
    reranking.model(str(directory))
    startup_ms = round(1000 * (perf_counter() - start), 1)
    dataset = json.loads(
        (ROOT / "evals/document_evidence.json").read_text(encoding="utf-8")
    )
    artifacts = []
    for artifact in dataset["artifacts"]:
        padding = "内部归档，无采购结论。"
        n = artifact["padding"]
        artifacts.append(
            {
                **artifact,
                "text": (padding * (n // len(padding) + 1))[:n] + artifact["text"],
            }
        )
    rows = []
    for item in dataset["queries"]:
        if not item["gold"]:
            # Negative queries require a relevance threshold and are outside rank-only evaluation.
            continue
        query = QUESTIONS[item["id"]]
        baseline = retrieval.search(artifacts, query, limit=reranking.CANDIDATE_LIMIT)
        start = perf_counter()
        ranked = reranking.reorder(query, baseline)
        if len(ranked) >= 2 and not all("rerank_score" in hit for hit in ranked):
            raise RuntimeError(f"Reranking did not run for {item['id']}")
        rows.append(
            {
                "case": item["id"],
                "candidates": len(baseline),
                "milliseconds": round(1000 * (perf_counter() - start), 1),
                "lexical": grade(baseline, item["gold"]),
                "lexical_then_bge": grade(ranked, item["gold"]),
            }
        )
    averages = {
        mode: {
            metric: round(sum(row[mode][metric] for row in rows) / len(rows), 4)
            for metric in ("reciprocal_rank", "recall_at_3")
        }
        for mode in ("lexical", "lexical_then_bge")
    }
    return {
        "fixture": "document_evidence.json",
        "scope": "synthetic lexical candidate ranking, excludes no-answer query",
        "model": reranking.MODEL_NAME,
        "weight_revision": reranking.MODEL_REVISION,
        "weight_sha256": sha,
        "scoring_characters": reranking.SCORING_CHARACTERS,
        "load_ms": startup_ms,
        "mean": averages,
        "cases": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.model_dir.resolve())
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
