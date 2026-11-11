"""检索协议、租户过滤、原文回读与索引发布边界，不将模拟向量当成模型验收。"""

import hashlib
import json
import httpx
import pytest
from fastapi import HTTPException
from procurepilot.search_index import SearchIndex, generation, DIMENSION
from procurepilot import indexing
from procurepilot import reranking
from procurepilot.storage import add, records


ARTIFACT = {
    "id": "source-a",
    "filename": "parameters.txt",
    "text": "USB-C 扩展坞，质保三年。",
}


def encoder(texts):
    return [[1.0] + [0.0] * (DIMENSION - 1) for _ in texts]


def response_handler(foreign=False, corrupt=False):
    def handle(request):
        body = json.loads(request.content)
        if "knn" in body["query"]:
            filters = body["query"]["knn"]["embedding"]["filter"]["bool"]["filter"]
        else:
            filters = body["query"]["bool"]["filter"]
        assert filters == [
            {"term": {"workspace_id": "workspace-a"}},
            {"term": {"generation": generation([ARTIFACT])}},
        ]
        source = {
            "workspace_id": "workspace-b" if foreign else "workspace-a",
            "generation": generation([ARTIFACT]),
            "artifact_id": ARTIFACT["id"],
            "offset": 0,
            "end_offset": len(ARTIFACT["text"]),
            "content_sha256": "bad"
            if corrupt
            else hashlib.sha256(ARTIFACT["text"].encode()).hexdigest(),
        }
        return httpx.Response(
            200, json={"hits": {"hits": [{"_id": "chunk-a", "_source": source}]}}
        )

    return handle


def test_each_recall_route_is_filtered_and_reads_original():
    index = SearchIndex(httpx.MockTransport(response_handler()), encoder)
    hits = index.search("workspace-a", [ARTIFACT], "扩展坞质保")
    assert (
        len(hits) == 1
        and hits[0]["snippet"] == ARTIFACT["text"]
        and hits[0]["untrusted"]
    )


def test_reranker_reorders_verified_text_and_falls_back_on_invalid_scores(monkeypatch):
    hits = [
        {"snippet": text, "source": text, "method": "hybrid", "score": 1.0}
        for text in ("旧版质保一年", "现行条款质保三年")
    ]

    class Ranker:
        def rerank(self, query, passages, batch_size):
            assert query == "当前质保" and passages == [hit["snippet"] for hit in hits]
            return [0.1, 0.9]

    monkeypatch.setenv("RERANK_MODEL_DIR", "fixture")
    monkeypatch.setattr(reranking, "model", lambda _: Ranker())
    result = reranking.reorder("当前质保", hits)
    assert [row["source"] for row in result] == [hits[1]["source"], hits[0]["source"]]
    assert result[0]["method"] == "hybrid_bge_int8" and "rerank_score" not in hits[0]

    monkeypatch.setattr(Ranker, "rerank", lambda *args, **kwargs: [float("nan"), 0.9])
    assert reranking.reorder("当前质保", hits) == hits


@pytest.mark.parametrize(
    "foreign,corrupt,expected", [(True, False, 503), (False, True, 409)]
)
def test_cross_scope_or_stale_index_fails_closed(foreign, corrupt, expected):
    index = SearchIndex(
        httpx.MockTransport(response_handler(foreign, corrupt)), encoder
    )
    with pytest.raises(HTTPException) as error:
        index.search("workspace-a", [ARTIFACT], "质保")
    assert error.value.status_code == expected


def test_partial_bulk_write_cannot_be_published():
    def handle(request):
        if request.method == "HEAD":
            return httpx.Response(200)
        return httpx.Response(200, json={"errors": True, "items": []})

    with pytest.raises(HTTPException):
        SearchIndex(httpx.MockTransport(handle), encoder).build(
            "workspace-a", [ARTIFACT]
        )


def test_index_enqueue_idempotent_and_stale_input_not_published(env):
    _, db, w = env
    with db.transaction(w) as s:
        first = indexing.enqueue(s, w, "test")
        assert indexing.enqueue(s, w, "test")["id"] == first["id"]
        add(
            s,
            w,
            "artifact",
            {**ARTIFACT, "characters": len(ARTIFACT["text"]), "untrusted": True},
        )

    class MustNotBuild:
        def build(self, *args):
            raise AssertionError("stale index should not run")

    assert not indexing.run_once(db, MustNotBuild())
    with db.transaction(w) as s:
        assert records(s, w, "index_job")[-1].data["state"] == "stale"


def test_index_publishes_only_after_success_and_does_not_leak_lease(env):
    c, db, w = env
    with db.transaction(w) as s:
        indexing.enqueue(s, w, "test")

    class Ready:
        def build(self, wid, artifacts, progress):
            progress(0, 0)
            return generation(artifacts), 0

    assert indexing.run_once(db, Ready())
    result = c.get(f"/api/w/{w}/evidence/index").json()[-1]
    assert result["state"] == "ready" and "lease_token" not in result
    snapshot = c.get(f"/api/w/{w}/snapshot").json()["index_job"][-1]
    assert "lease_token" not in snapshot


def test_failed_index_does_not_publish_or_expose_error_text(env):
    c, db, w = env
    with db.transaction(w) as s:
        indexing.enqueue(s, w, "test")

    class Broken:
        def build(self, *args):
            raise RuntimeError("private upstream credentials")

    assert indexing.run_once(db, Broken())
    result = c.get(f"/api/w/{w}/evidence/index").json()[-1]
    assert result["state"] == "failed" and "private" not in str(result)


def test_indexing_requires_write_role(env):
    c, _, w = env
    c.headers["Authorization"] = "Bearer demo-viewer"
    assert c.post(f"/api/w/{w}/evidence/index", json={}).status_code == 403
