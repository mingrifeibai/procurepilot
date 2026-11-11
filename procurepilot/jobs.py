"""数据库持久化分析队列：抢占、租约、执行隔离和保守恢复。

SQLite 与 PostgreSQL 共用工作区事务锁。消息只负责调度，analysis/model_call
才是权威状态。失去响应的外部调用永不自动重发，避免把重试误当成恰好一次。
调度可使用数据库轮询或 Redis Streams，两者共享同一份租约账本。
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import select
from .storage import Record, records, view
from .service import get, fail, audit
from .models import request_timeout_seconds


def utcnow():
    return datetime.now(timezone.utc)


def public(task):
    """不向浏览器暴露完整模型上下文或内部执行租约令牌。"""
    return {k: v for k, v in view(task).items() if k not in ("context", "worker_token")}


def check_owner(task, token, renew=False):
    """每次新模型调用前校验执行权，阻止租约过期的旧进程继续调用。"""
    data = task.data
    if token is None:
        if data.get("dispatch_state") in ("queued", "leased"):
            fail("任务已交给后台执行，请查询进度或取消")
        return
    if data.get("worker_token") != token or data.get("dispatch_state") != "leased":
        fail("后台任务执行权已失效")
    if datetime.fromisoformat(data["worker_lease_until"]) <= utcnow():
        fail("后台任务租约已过期，等待恢复检查")
    if renew:
        task.data = {
            **data,
            "worker_lease_until": (
                utcnow() + timedelta(seconds=request_timeout_seconds() + 60)
            ).isoformat(),
        }


def claim(store, target=None):
    """扫描后再在工作区锁内竞争；不同进程至多一个获得同一有效租约。

    只恢复没有未决调用的检查点；发现过期未决调用时记录不确定用量并停止。
    已完成角色保留，恢复不会延长原任务时限或重置预算。
    """
    with store.engine.connect() as conn:
        # SQL 过滤队列状态，避免每轮加载所有历史分析的完整上下文。
        statement = select(Record.id, Record.workspace_id)
        if target:
            statement = statement.where(
                Record.workspace_id == target[0], Record.id == target[1]
            )
        candidates = conn.execute(
            statement.where(
                Record.kind == "analysis",
                Record.data["dispatch_state"].as_string().in_(("queued", "leased")),
            ).order_by(Record.created_at, Record.id)
        ).all()
    for aid, wid in candidates:
        with store.transaction(wid) as s:
            task = get(s, wid, "analysis", aid)
            data = task.data
            if data.get("dispatch_state") not in ("queued", "leased"):
                continue
            if (
                data.get("dispatch_state") == "leased"
                and datetime.fromisoformat(data["worker_lease_until"]) > utcnow()
            ):
                continue
            pending = [
                c
                for c in records(s, wid, "model_call")
                if c.data["analysis_id"] == aid and c.data["state"] == "pending"
            ]
            if any(
                datetime.fromisoformat(c.data["lease_until"]) > utcnow()
                for c in pending
            ):
                continue
            if pending:
                for call in pending:
                    call.data = {
                        **call.data,
                        "state": "uncertain",
                        "error_type": "WorkerInterrupted",
                    }
                task.data = {
                    **data,
                    "dispatch_state": "done",
                    "state": "cancelled"
                    if data["state"] == "cancelled"
                    else "tool_unavailable",
                    "uncertain_usage": True,
                    "error": {
                        "hint": "执行进程中断，远端调用结果未知；请核对供应商用量，不自动重发"
                    },
                }
                audit(s, wid, "worker", "analysis.uncertain_after_interruption", aid)
                continue
            if data["state"] not in ("queued", "running"):
                task.data = {**data, "dispatch_state": "done"}
                continue
            if datetime.fromisoformat(data["deadline"]) <= utcnow():
                task.data = {
                    **data,
                    "state": "budget_exhausted",
                    "dispatch_state": "done",
                    "budget_reason": "deadline",
                }
                continue
            token = str(uuid4())
            task.data = {
                **data,
                "state": "running",
                "dispatch_state": "leased",
                "worker_token": token,
                "worker_lease_until": (
                    utcnow() + timedelta(seconds=request_timeout_seconds() + 60)
                ).isoformat(),
                "dispatch_attempts": data.get("dispatch_attempts", 0) + 1,
            }
            audit(s, wid, "worker", "analysis.claimed", aid)
            return wid, aid, token, data["actor"]
    return None


def run_once(store, model=None, target=None):
    """执行一条任务。异常只保存类型，禁止输出可能包含密钥的异常正文。"""
    claimed = claim(store, target)
    if not claimed:
        return False
    wid, aid, token, actor = claimed
    from . import analysis

    try:
        from .observability import span, trace_id

        with span("analysis.run", {"analysis.id": aid}):
            identifier = trace_id()
            if identifier:
                with store.transaction(wid) as s:
                    task = get(s, wid, "analysis", aid)
                    task.data = {**task.data, "trace_id": identifier}
            analysis.run(
                store, wid, actor, model=model, task_id=aid, worker_token=token
            )
    except Exception as exc:
        with store.transaction(wid) as s:
            task = get(s, wid, "analysis", aid)
            if task.data.get("worker_token") == token:
                # 保留 leased，等待租约到期后按 model_call 账本裁决是否可恢复。
                task.data = {**task.data, "worker_error_type": type(exc).__name__}
        return True
    with store.transaction(wid) as s:
        task = get(s, wid, "analysis", aid)
        if task.data.get("worker_token") == token:
            task.data = {**task.data, "dispatch_state": "done"}
    return True


def requeue(session, wid, task, actor):
    """用户显式恢复时只重新排队，不延长原时限，不自动重试未知结果。"""
    check_owner(task, None)
    data = task.data
    if data["state"] not in (
        "running",
        "waiting_provider",
        "waiting_response",
    ) or data.get("uncertain_usage"):
        fail("此任务状态不能恢复")
    if (
        data["state"] == "waiting_provider"
        and datetime.fromisoformat(data["retry_at"]) > utcnow()
    ):
        fail("供应商冷却时间未到，请稍后恢复")
    task.data = {
        **data,
        "state": "queued",
        "dispatch_state": "queued",
        "worker_token": None,
    }
    audit(session, wid, actor, "analysis.requeued", task.id)
    from .streams import schedule

    schedule(session, wid, task.id)
    return public(task)
