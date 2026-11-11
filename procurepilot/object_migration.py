"""将已有本地原件分批迁移到配置好的 S3。"""

from fastapi import HTTPException
from sqlalchemy import or_, select

from .objects import LocalObjects, S3Objects
from .service import audit, get
from .storage import Record


def migrate(store, limit=100, *, apply=False):
    """完整写入和回读后提交 SQL 引用。失败可重试，本地原件留作回溯。"""
    if not 1 <= limit <= 1000:
        raise ValueError("每批对象上限需在 1 至 1000 之间")
    object_field = Record.data["object"]
    backend = object_field["backend"].as_string()
    with store.engine.connect() as connection:
        rows = connection.execute(
            select(Record.workspace_id, Record.id)
            .where(
                Record.kind == "artifact",
                object_field["key"].as_string().is_not(None),
                or_(backend == "local", backend.is_(None)),
            )
            .order_by(Record.created_at, Record.id)
            .limit(limit)
        ).all()
    result = {"selected": len(rows), "moved": 0, "changed": 0, "failed": []}
    if not apply or not rows:
        return result
    local, remote = LocalObjects(), S3Objects()
    for wid, aid in rows:
        with store.transaction(wid) as session:
            artifact = get(session, wid, "artifact", aid)
            previous = artifact.data.get("object")
            if not previous or previous.get("backend", "local") != "local":
                result["changed"] += 1
                continue
        try:
            content = local.get(previous)
            uploaded = remote.put(content)
            remote.get(uploaded)
        except HTTPException:
            result["failed"].append(aid)
            continue
        with store.transaction(wid) as session:
            artifact = get(session, wid, "artifact", aid)
            if artifact.data.get("object") != previous:
                result["changed"] += 1
                continue
            artifact.data = {**artifact.data, "object": uploaded}
            audit(session, wid, "maintenance", "artifact.object_migrated", aid)
            result["moved"] += 1
    return result
