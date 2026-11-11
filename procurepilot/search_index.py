"""本地中文向量 + OpenSearch词法/向量双路召回与RRF重排。

索引仅是派生数据。查询固定绑定工作区及已发布代次，命中必须用SQL原文
校验哈希并回读，索引正文不能替换权威附件。模型运行在本机CPU，无模型API费用。
"""

import hashlib
import json
import math
import os
import re
from functools import lru_cache
from pathlib import Path
from threading import Lock
import httpx
from .retrieval import chunks, tokenize
from .reranking import reorder
from .service import fail
from .settings import setting

MODEL = "BAAI/bge-small-zh-v1.5"
MODEL_REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
DIMENSION = 512
_embedding_lock = Lock()


def embedding_cache():
    return Path(
        os.getenv("EMBEDDING_CACHE_DIR")
        or Path(__file__).resolve().parents[1] / ".local/embedding-cache"
    )


@lru_cache(maxsize=1)
def local_model():
    """延迟加载，普通业务启动不触发下载；线程数限制为2以保护开发电脑。"""
    from fastembed import TextEmbedding
    from huggingface_hub import snapshot_download

    cache = embedding_cache()
    model_path = snapshot_download(
        repo_id="Qdrant/bge-small-zh-v1.5",
        revision=MODEL_REVISION,
        cache_dir=str(cache),
        token=False,
        local_files_only=os.getenv("ALLOW_MODEL_DOWNLOAD") != "1",
        allow_patterns=["model_optimized.onnx", "*.json", "vocab.txt"],
    )
    return TextEmbedding(
        model_name=MODEL,
        threads=2,
        cache_dir=str(cache),
        specific_model_path=model_path,
        providers=["CPUExecutionProvider"],
    )


def embed(texts):
    try:
        with _embedding_lock:
            vectors = [v.tolist() for v in local_model().embed(texts)]
        if len(vectors) != len(texts):
            raise ValueError()
        result = []
        for vector in vectors:
            if len(vector) != DIMENSION or any(not math.isfinite(v) for v in vector):
                raise ValueError()
            norm = math.sqrt(sum(v * v for v in vector))
            if norm == 0:
                raise ValueError()
            result.append([v / norm for v in vector])
        return result
    except Exception:
        fail("本地向量模型尚未就绪，请查看索引任务诊断和模型缓存", 503)


def generation(artifacts):
    """稳定的输入代次；附件或嵌入配置变化后旧索引立即失效。"""
    manifest = sorted(
        (a["id"], hashlib.sha256(a["text"].encode()).hexdigest()) for a in artifacts
    )
    return hashlib.sha256(
        json.dumps(
            [MODEL, MODEL_REVISION, DIMENSION, 400, 100, manifest], sort_keys=True
        ).encode()
    ).hexdigest()


class SearchIndex:
    def __init__(self, transport=None, encoder=None):
        self.url = setting("OPENSEARCH_URL", "http://127.0.0.1:59200").rstrip("/")
        self.index = os.getenv("OPENSEARCH_INDEX", "procurepilot-evidence-v1")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,100}", self.index):
            raise ValueError("索引名称格式无效")
        self.transport = transport
        self.encoder = encoder or embed

    def request(self, method, path, body=None, raw=None):
        """禁止隐式代理与重定向，服务端凭据只来自环境，不出现在诊断中。"""
        user, password = os.getenv("OPENSEARCH_USER"), os.getenv("OPENSEARCH_PASSWORD")
        try:
            with httpx.Client(
                timeout=30,
                trust_env=False,
                transport=self.transport,
                auth=(user, password) if user and password else None,
                headers={"Accept-Encoding": "identity"},
            ) as client:
                response = client.request(
                    method,
                    self.url + path,
                    json=body if raw is None else None,
                    content=raw,
                    headers={"Content-Type": "application/x-ndjson"}
                    if raw is not None
                    else None,
                )
                if response.status_code == 404 and method == "HEAD":
                    return None
                response.raise_for_status()
                payload = response.json() if response.content else {}
                if not isinstance(payload, dict):
                    raise ValueError()
                return payload
        except (httpx.HTTPError, ValueError):
            fail("检索服务请求失败，请检查OpenSearch连接与配置", 503)

    def ensure(self):
        if self.request("HEAD", "/" + self.index) is not None:
            return
        self.request(
            "PUT",
            "/" + self.index,
            {
                "settings": {
                    "index.knn": True,
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                },
                "mappings": {
                    "dynamic": "strict",
                    "properties": {
                        "workspace_id": {"type": "keyword"},
                        "generation": {"type": "keyword"},
                        "artifact_id": {"type": "keyword"},
                        "offset": {"type": "integer"},
                        "end_offset": {"type": "integer"},
                        "content_sha256": {"type": "keyword"},
                        "tokens": {"type": "text"},
                        "embedding": {
                            "type": "knn_vector",
                            "dimension": DIMENSION,
                            "method": {
                                "name": "hnsw",
                                "engine": "faiss",
                                "space_type": "innerproduct",
                            },
                        },
                    },
                },
            },
        )

    def write_batch(self, wid, revision, segments):
        vectors = self.encoder([s["snippet"] for s in segments])
        if len(vectors) != len(segments) or any(
            len(v) != DIMENSION or any(not math.isfinite(x) for x in v) for v in vectors
        ):
            fail("索引向量维度或数值无效", 503)
        lines = []
        for segment, vector in zip(segments, vectors):
            identity = hashlib.sha256(
                f"{wid}:{revision}:{segment['artifact_id']}:{segment['offset']}".encode()
            ).hexdigest()
            lines.append(json.dumps({"index": {"_index": self.index, "_id": identity}}))
            lines.append(
                json.dumps(
                    {
                        "workspace_id": wid,
                        "generation": revision,
                        **{
                            k: segment[k]
                            for k in (
                                "artifact_id",
                                "offset",
                                "end_offset",
                                "content_sha256",
                            )
                        },
                        "tokens": " ".join(tokenize(segment["snippet"])),
                        "embedding": vector,
                    },
                    ensure_ascii=False,
                )
            )
        payload = self.request("POST", "/_bulk", raw="\n".join(lines) + "\n")
        if payload.get("errors") is not False or len(payload.get("items", [])) != len(
            segments
        ):
            fail("部分证据索引写入失败，尚未发布该索引代次", 503)

    def build(self, wid, artifacts, progress=None):
        segments = chunks(artifacts, chunk_size=400, overlap=100)
        revision = generation(artifacts)
        self.ensure()
        for start in range(0, len(segments), 16):
            self.write_batch(wid, revision, segments[start : start + 16])
            if progress:
                progress(min(start + 16, len(segments)), len(segments))
        self.request("POST", f"/{self.index}/_refresh")
        return revision, len(segments)

    def search(self, wid, artifacts, query, limit=20):
        """每一路都在引擎内过滤，回读时再次验证租户、代次、偏移和原文哈希。"""
        if not query.strip() or len(query) > 200 or not 1 <= limit <= 20:
            fail("检索参数超出范围", 422)
        revision = generation(artifacts)
        filters = [{"term": {"workspace_id": wid}}, {"term": {"generation": revision}}]
        lexical = {
            "size": 30,
            "_source": {"excludes": ["embedding", "tokens"]},
            "query": {
                "bool": {
                    "filter": filters,
                    "must": [{"match": {"tokens": " ".join(tokenize(query))}}],
                }
            },
        }
        vector = {
            "size": 30,
            "_source": {"excludes": ["embedding", "tokens"]},
            "query": {
                "knn": {
                    "embedding": {
                        "vector": self.encoder([query])[0],
                        "k": 30,
                        "filter": {"bool": {"filter": filters}},
                    }
                }
            },
        }
        rankings = []
        for body in (lexical, vector):
            payload = self.request("POST", f"/{self.index}/_search", body)
            hits = payload.get("hits", {}).get("hits")
            if not isinstance(hits, list) or payload.get("_shards", {}).get(
                "failed", 0
            ):
                fail("检索结果不完整或部分分片失败", 503)
            if any(
                not isinstance(hit, dict)
                or not isinstance(hit.get("_id"), str)
                or not isinstance(hit.get("_source"), dict)
                for hit in hits
            ):
                fail("检索结果结构无效", 503)
            rankings.append(hits)
        by_id = {}
        scores = {}
        for ranking in rankings:
            for rank, hit in enumerate(ranking, 1):
                by_id[hit["_id"]] = hit["_source"]
                scores[hit["_id"]] = scores.get(hit["_id"], 0) + 1 / (60 + rank)
        documents = {a["id"]: a for a in artifacts}
        results = []
        terms = set(tokenize(query))
        for identity, source in by_id.items():
            if (
                source.get("workspace_id") != wid
                or source.get("generation") != revision
            ):
                fail("检索结果隔离校验失败", 503)
            artifact = documents.get(source.get("artifact_id"))
            if artifact is None or hashlib.sha256(
                artifact["text"].encode()
            ).hexdigest() != source.get("content_sha256"):
                fail("检索结果与当前原文不一致，请重建索引", 409)
            start, end = source.get("offset"), source.get("end_offset")
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(artifact["text"])
                or end - start > 800
            ):
                fail("检索结果原文偏移无效", 503)
            snippet = artifact["text"][start:end]
            matched = terms & set(tokenize(snippet))
            coverage = len(matched) / max(1, len(terms))
            results.append(
                {
                    "artifact_id": artifact["id"],
                    "filename": artifact["filename"],
                    "offset": start,
                    "end_offset": end,
                    "snippet": snippet,
                    "source": f"artifact:{artifact['id']}:offset:{start}",
                    "content_sha256": source["content_sha256"],
                    "untrusted": True,
                    "score": scores[identity] * (0.5 + coverage),
                    "method": "opensearch_bm25_bge_rrf_coverage_v1",
                    "matched_terms": sorted(matched),
                }
            )
        ranked = sorted(results, key=lambda h: (-h["score"], h["source"]))
        return reorder(query, ranked)[:limit]
