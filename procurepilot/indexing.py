"""检索索引后台构建：外部写入幂等，完整代次在SQL事务内发布。"""

from datetime import timedelta, datetime
from uuid import uuid4
from sqlalchemy import select
from .storage import Record, add, view, records
from .service import get, fail, audit
from .jobs import utcnow
from .search_index import SearchIndex, generation


def enqueue(session, wid, actor):
    artifacts = [view(a) for a in records(session, wid, "artifact")]
    revision = generation(artifacts)
    for item in reversed(records(session, wid, "index_job")):
        if item.data["generation"] == revision and item.data["state"] in (
            "queued",
            "running",
            "ready",
        ):
            return view(item)
    job = add(
        session,
        wid,
        "index_job",
        {
            "generation": revision,
            "state": "queued",
            "attempts": 0,
            "actor": actor,
            "completed_chunks": 0,
            "total_chunks": None,
        },
    )
    audit(session, wid, actor, "evidence.index_requested", job.id)
    return view(job)


def run_once(store, index=None):
    """租约竞争后释放事务执行编码；旧执行者失去令牌后不能发布索引。"""
    with store.engine.connect() as conn:
        candidates = conn.execute(
            select(Record.id, Record.workspace_id)
            .where(
                Record.kind == "index_job",
                Record.data["state"].as_string().in_(("queued", "running")),
            )
            .order_by(Record.created_at, Record.id)
        ).all()
    for jid, wid in candidates:
        with store.transaction(wid) as s:
            job = get(s, wid, "index_job", jid)
            if job.data["state"] not in ("queued", "running"):
                continue
            if (
                job.data["state"] == "running"
                and datetime.fromisoformat(job.data["lease_until"]) > utcnow()
            ):
                continue
            if job.data["attempts"] >= 3:
                job.data = {**job.data, "state": "failed", "error_type": "RetryLimit"}
                continue
            artifacts = [view(a) for a in records(s, wid, "artifact")]
            if generation(artifacts) != job.data["generation"]:
                job.data = {**job.data, "state": "stale"}
                continue
            token = str(uuid4())
            job.data = {
                **job.data,
                "state": "running",
                "attempts": job.data["attempts"] + 1,
                "lease_token": token,
                "lease_until": (utcnow() + timedelta(minutes=5)).isoformat(),
            }

        def progress(done, total):
            with store.transaction(wid) as s:
                current = get(s, wid, "index_job", jid)
                if (
                    current.data.get("lease_token") != token
                    or current.data["state"] != "running"
                ):
                    fail("索引执行权已失效")
                if (
                    generation([view(a) for a in records(s, wid, "artifact")])
                    != current.data["generation"]
                ):
                    fail("附件已变更，请重新构建索引")
                current.data = {
                    **current.data,
                    "completed_chunks": done,
                    "total_chunks": total,
                    "lease_until": (utcnow() + timedelta(minutes=5)).isoformat(),
                }

        try:
            revision, count = (index or SearchIndex()).build(wid, artifacts, progress)
            with store.transaction(wid) as s:
                current = get(s, wid, "index_job", jid)
                if current.data.get("lease_token") != token:
                    return True
                state = (
                    "ready"
                    if generation([view(a) for a in records(s, wid, "artifact")])
                    == revision
                    else "stale"
                )
                current.data = {
                    **current.data,
                    "state": state,
                    "completed_chunks": count,
                    "total_chunks": count,
                }
                audit(s, wid, "worker", "evidence.index_" + state, jid)
        except Exception as exc:
            with store.transaction(wid) as s:
                current = get(s, wid, "index_job", jid)
                if current.data.get("lease_token") == token:
                    current.data = {
                        **current.data,
                        "state": "failed",
                        "error_type": type(exc).__name__,
                    }
        return True
    return False


def search(session, wid, query, limit=20):
    """只有与当前附件一致的已发布代次可检索。"""
    artifacts = [view(a) for a in records(session, wid, "artifact")]
    revision = generation(artifacts)
    if not any(
        j.data["state"] == "ready" and j.data["generation"] == revision
        for j in records(session, wid, "index_job")
    ):
        fail("当前附件尚未建立检索索引，请先构建索引", 409)
    return SearchIndex().search(wid, artifacts, query, limit)
