"""显式续验用量已确认的响应契约失败，不重置费用、Token 或成功角色。"""

from datetime import datetime, timedelta, timezone
from . import analysis
from .service import get, fail, audit
from .models import REVIEWABLE_RESPONSE_ISSUES


def continue_truncated(
    store, wid, aid, actor, seconds=300, model=None, prepare_only=False
):
    if not 30 <= seconds <= 300:
        fail("追加时限必须为30至300秒", 422)
    with store.transaction(wid) as s:
        task = get(s, wid, "analysis", aid)
        from .jobs import check_owner

        check_owner(task, None)
        data = task.data
        if (
            data["state"] not in ("tool_unavailable", "waiting_response")
            or data.get("uncertain_usage") is not False
            or data.get("error", {}).get("response_issue")
            not in REVIEWABLE_RESPONSE_ISSUES
        ):
            fail("仅允许继续用量已核对且需要修正的响应")
        if (
            data["tokens"] >= data["max_tokens"]
            or data.get("attempts", 0) >= data["max_steps"] + 3
        ):
            fail("原任务总预算已耗尽，不能通过延长时间重置预算")
        old_deadline = data["deadline"]
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
        task.data = {**data, "state": "waiting_response", "deadline": deadline}
        audit(s, wid, actor, "analysis.continuation_authorized", aid)
        from .storage import add

        add(
            s,
            wid,
            "continuation",
            {
                "analysis_id": aid,
                "actor": actor,
                "old_deadline": old_deadline,
                "new_deadline": deadline,
                "tokens_already_used": data["tokens"],
            },
        )
        if prepare_only:
            from .jobs import requeue

            return requeue(s, wid, task, actor)
    return analysis.run(store, wid, actor, model=model, task_id=aid)
