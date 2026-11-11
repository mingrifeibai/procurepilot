"""推送工作区分析变更。游标是当前状态指纹，断线后可回读权威快照。"""

import asyncio
from hashlib import sha256
import json
import time

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .storage import Record


WINDOW_SECONDS = 25
INTERVAL_SECONDS = 0.8


def revision(engine, wid):
    scope = (Record.workspace_id == wid, Record.kind == "analysis")
    recent = (
        select(Record.id)
        .where(*scope)
        .order_by(Record.created_at.desc(), Record.id.desc())
        .limit(64)
    )
    state = Record.data["state"].as_string()
    fields = ("dispatch_state", "steps", "attempts", "tokens")
    statement = (
        select(Record.id, state, *(Record.data[field].as_string() for field in fields))
        .where(*scope, or_(Record.id.in_(recent), state.in_(("queued", "running"))))
        .order_by(Record.id)
    )
    # 单次只读事务不持有工作区写锁。流等待期间也不占用数据库连接。
    with Session(engine) as session:
        rows = [tuple(row) for row in session.execute(statement)]
    return sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


async def events(engine, wid, cursor):
    """重复游标只发心跳。流有界，客户端重连时重新校验工作区权限。"""
    deadline = time.monotonic() + WINDOW_SECONDS
    heartbeat = time.monotonic()
    while True:
        current = await run_in_threadpool(revision, engine, wid)
        if current != cursor:
            cursor = current
            yield f"id: {current}\nevent: analysis_changed\ndata: {{}}\n\n"
        elif time.monotonic() >= heartbeat:
            yield ": heartbeat\n\n"
            heartbeat = time.monotonic() + 10
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(INTERVAL_SECONDS, remaining))
