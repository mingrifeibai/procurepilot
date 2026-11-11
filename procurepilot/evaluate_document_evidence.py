"""有界证据预览对照，按独立标注的原文和附件 ID 衡量保留率。"""

import argparse
import hashlib
import json
from pathlib import Path

from .retrieval import focus_excerpt, search


PADDING = " 内部留档，无有效采购结论。 "
PREVIEW_LIMIT = 400


def evaluate(dataset):
    artifacts = [
        {
            "id": item["id"],
            "text": (PADDING * (item["padding"] // len(PADDING) + 1))[: item["padding"]]
            + item["text"],
        }
        for item in dataset["artifacts"]
    ]
    originals = {item["id"]: item["text"] for item in artifacts}
    full = [
        {"artifact_id": item["id"], "preview": item["text"], "preview_offset": 0}
        for item in artifacts
    ]
    reports = {"full_documents": [], "fixed_prefix": [], "focused_excerpt": []}
    for case in dataset["queries"]:
        gold = case["gold"]
        for fact in gold:
            if not fact["quote"] or fact["quote"] not in originals[fact["artifact_id"]]:
                raise ValueError(f"原文标注无法回读：{case['id']}")
        hits = search(artifacts, case["query"], limit=2)
        variants = {
            "full_documents": full,
            "fixed_prefix": [
                {
                    "artifact_id": hit["artifact_id"],
                    "preview_offset": hit["offset"],
                    "preview": hit["snippet"][:PREVIEW_LIMIT],
                }
                for hit in hits
            ],
            "focused_excerpt": [
                {
                    "artifact_id": hit["artifact_id"],
                    **focus_excerpt(hit, case["query"], PREVIEW_LIMIT),
                }
                for hit in hits
            ],
        }
        for name, previews in variants.items():
            checks = [
                {
                    **fact,
                    "covered": any(
                        item["artifact_id"] == fact["artifact_id"]
                        and fact["quote"] in item["preview"]
                        for item in previews
                    ),
                }
                for fact in gold
            ]
            reports[name].append(
                {
                    "case": case["id"],
                    "checks": checks,
                    "excerpt_bytes": sum(
                        len(item["preview"].encode("utf-8")) for item in previews
                    ),
                    "returned_artifacts": list(
                        dict.fromkeys(item["artifact_id"] for item in previews)
                    ),
                    "offsets_valid": all(
                        originals[item["artifact_id"]][
                            item["preview_offset"] : item["preview_offset"]
                            + len(item["preview"])
                        ]
                        == item["preview"]
                        for item in previews
                    ),
                    "negative_empty": not previews
                    if not gold and name != "full_documents"
                    else None,
                }
            )

    result = {}
    for name, rows in reports.items():
        facts = [fact for row in rows for fact in row["checks"]]
        result[name] = {
            "covered_facts": sum(fact["covered"] for fact in facts),
            "total_facts": len(facts),
            "excerpt_bytes": sum(row["excerpt_bytes"] for row in rows if row["checks"]),
            "offsets_valid": all(row["offsets_valid"] for row in rows),
            "correct_empty_negatives": sum(
                row["negative_empty"] is True for row in rows
            ),
            "rows": rows,
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="evals/document_evidence.json", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raw = args.dataset.read_bytes()
    result = {
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "model_calls": 0,
        "top_k": 2,
        "preview_character_limit": PREVIEW_LIMIT,
        "methods": evaluate(json.loads(raw)),
    }
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for method, row in result["methods"].items():
        print(
            f"{method}: {row['covered_facts']}/{row['total_facts']} facts, {row['excerpt_bytes']} bytes"
        )


if __name__ == "__main__":
    main()
