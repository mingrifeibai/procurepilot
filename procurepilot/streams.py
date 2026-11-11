"""事务Outbox → Redis Streams → 数据库租约执行。

消息只携带内部标识，模型输入、权限与预算从SQL读取。采用至少一次投递；
外部模型未知结果的处理仍由model_call账本决定，消息重投不等于模型重试。
Redis不可用或消息过期时，Worker保留数据库补偿扫描，避免调度丢失。
"""

from datetime import timedelta
import os
import re
from uuid import UUID, uuid4
from sqlalchemy import select
from fastapi import HTTPException
from .storage import Record, add
from .service import get
from .jobs import utcnow
from .settings import redis_url

GROUP = "procurepilot-workers-v1"


def schedule(session, wid, aid):
    """与分析入队/重新排队在同一SQL事务提交，不在业务事务内访问Redis。"""
    return add(
        session,
        wid,
        "queue_outbox",
        {
            "analysis_id": aid,
            "state": "pending",
            "attempts": 0,
            "event_type": "analysis.ready",
        },
    )


class StreamQueue:
    """单Worker消费者。处理后确认，崩溃未确认消息可在空闲超时后被认领。"""

    def __init__(self, store, client=None, consumer=None, idle_ms=180000):
        import redis

        self.store = store
        self.key = os.getenv("REDIS_STREAM_KEY", "procurepilot:analysis:v1")
        if not re.fullmatch(r"[A-Za-z0-9:_.-]{1,100}", self.key):
            raise ValueError("REDIS_STREAM_KEY格式无效")
        self.client = client or redis.Redis.from_url(
            redis_url(),
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        self.consumer = consumer or str(uuid4())
        self.idle_ms = idle_ms

    def ensure_group(self):
        from redis.exceptions import ResponseError

        try:
            self.client.xgroup_create(self.key, GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if not str(exc).startswith("BUSYGROUP"):
                raise

    def publish(self, limit=100):
        """发送成功后再标记SQL；两者之间崩溃可能重复，消费端按任务租约去重。"""
        with self.store.engine.connect() as conn:
            candidates = conn.execute(
                select(Record.id, Record.workspace_id)
                .where(
                    Record.kind == "queue_outbox",
                    Record.data["state"].as_string() == "pending",
                )
                .order_by(Record.created_at, Record.id)
                .limit(limit)
            ).all()
        published = 0
        for eid, wid in candidates:
            with self.store.transaction(wid) as s:
                event = get(s, wid, "queue_outbox", eid)
                data = event.data
                if data["state"] != "pending":
                    continue
                if data.get("retry_at") and utcnow().isoformat() < data["retry_at"]:
                    continue
                attempts = data["attempts"] + 1
                try:
                    mid = self.client.xadd(
                        self.key,
                        {
                            "event_id": eid,
                            "workspace_id": wid,
                            "analysis_id": data["analysis_id"],
                        },
                        maxlen=10000,
                        approximate=True,
                    )
                except Exception as exc:
                    event.data = {
                        **data,
                        "attempts": attempts,
                        "last_error_type": type(exc).__name__,
                        "retry_at": (
                            utcnow() + timedelta(seconds=min(60, 2 ** min(attempts, 6)))
                        ).isoformat(),
                    }
                    break
                event.data = {
                    **data,
                    "state": "published",
                    "attempts": attempts,
                    "stream_id": mid,
                }
                published += 1
        return published

    def consume_once(self, model=None):
        """先认领失联消费者的未决消息，再读取新消息；只确认已落定的任务。"""
        self.ensure_group()
        result = self.client.xautoclaim(
            self.key,
            GROUP,
            self.consumer,
            min_idle_time=self.idle_ms,
            start_id="0-0",
            count=1,
        )
        messages = result[1]
        if not messages:
            fresh = self.client.xreadgroup(
                GROUP, self.consumer, {self.key: ">"}, count=1
            )
            messages = fresh[0][1] if fresh else []
        if not messages:
            return False
        mid, fields = messages[0]
        try:
            wid, aid, eid = (
                str(UUID(fields[k]))
                for k in ("workspace_id", "analysis_id", "event_id")
            )
            with self.store.transaction(wid) as s:
                event = get(s, wid, "queue_outbox", eid)
                if event.data["analysis_id"] != aid:
                    raise ValueError("mismatched_event")
                get(s, wid, "analysis", aid)
        except (KeyError, ValueError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code != 404:
                raise
            # 死信只保留标识和原因，不复制不可信消息正文。
            self.client.xadd(
                self.key + ":dead",
                {"message_id": mid, "reason": "invalid_reference"},
                maxlen=1000,
            )
            self.client.xack(self.key, GROUP, mid)
            return True
        from .jobs import run_once

        run_once(self.store, model, target=(wid, aid))
        with self.store.transaction(wid) as s:
            task = get(s, wid, "analysis", aid)
            if task.data.get("dispatch_state") == "done":
                self.client.xack(self.key, GROUP, mid)
        return True
