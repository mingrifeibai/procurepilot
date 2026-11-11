"""使用本机交叉编码器对已校验的原文候选重排。"""

import logging
import math
import os
from functools import lru_cache
from pathlib import Path
from threading import Lock

from .retrieval import focus_excerpt


MODEL_NAME = "BAAI/bge-reranker-v2-m3-int8"
MODEL_REVISION = "6f5ff65298512715a1e669753bc754d2bc8f367b"
CANDIDATE_LIMIT = 16
SCORING_CHARACTERS = 256
_lock = Lock()
log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def model(directory):
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    path = Path(directory).resolve()
    if not (path / "onnx" / "model_int8.onnx").is_file():
        raise FileNotFoundError("本地重排模型未安装")
    return TextCrossEncoder(
        model_name=MODEL_NAME,
        specific_model_path=str(path),
        local_files_only=True,
        providers=["CPUExecutionProvider"],
        threads=2,
    )


def reorder(query, hits):
    """只重排前 16 条，模型不可用时按原融合排序返回。"""
    directory = os.getenv("RERANK_MODEL_DIR")
    if not directory or len(hits) < 2:
        return hits
    candidates = hits[:CANDIDATE_LIMIT]
    # 有并发请求时保留可用的融合结果，避免让一批 CPU 推理阻塞其他查询。
    if not _lock.acquire(blocking=False):
        return hits
    try:
        passages = [
            focus_excerpt(hit, query, limit=SCORING_CHARACTERS)["preview"]
            if len(hit["snippet"]) > SCORING_CHARACTERS
            else hit["snippet"]
            for hit in candidates
        ]
        scores = [
            float(score)
            for score in model(directory).rerank(query, passages, batch_size=4)
        ]
        if len(scores) != len(candidates) or not all(map(math.isfinite, scores)):
            raise ValueError("本机重排响应无效")
    except Exception as error:
        log.warning("本机重排未完成，使用融合排序：%s", type(error).__name__)
        return hits
    finally:
        _lock.release()
    ranked = sorted(
        zip(candidates, scores),
        key=lambda row: (-row[1], -row[0]["score"], row[0]["source"]),
    )
    return [
        {**hit, "rerank_score": score, "method": hit["method"] + "_bge_int8"}
        for hit, score in ranked
    ] + hits[CANDIDATE_LIMIT:]
