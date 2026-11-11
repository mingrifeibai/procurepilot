"""工作区确认偏好：版本追加、有效期与停用，不覆盖当前任务明确要求。"""

from datetime import datetime, timedelta, timezone
from .storage import add, records, view
from .service import audit, fail, get


def current(session, wid, include_inactive=False):
    latest = {}
    for row in records(session, wid, "preference"):
        latest[row.data["key"]] = row
    result = []
    for row in latest.values():
        item = view(row)
        state = item.get("state", "active")
        if (
            state == "active"
            and item.get("expires_at")
            and datetime.fromisoformat(item["expires_at"]) <= datetime.now(timezone.utc)
        ):
            state = "expired"
        item["state"] = state
        if include_inactive or state == "active":
            result.append(item)
    return sorted(result, key=lambda x: x["key"])


def save(session, wid, body, actor):
    if not body.confirmed:
        fail("长期偏好必须明确确认", 422)
    previous = [
        r for r in records(session, wid, "preference") if r.data["key"] == body.key
    ]
    data = {
        "key": body.key,
        "value": body.value,
        "confirmed": True,
        "state": "active",
        "version": len(previous) + 1,
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
        ).isoformat()
        if body.expires_in_days
        else None,
    }
    row = add(session, wid, "preference", data)
    audit(session, wid, actor, "preference.confirmed", row.id)
    return view(row)


def deactivate(session, wid, pid, actor):
    row = get(session, wid, "preference", pid)
    latest = next(p for p in current(session, wid, True) if p["key"] == row.data["key"])
    if latest["state"] == "inactive":
        return latest
    if latest["id"] != pid:
        fail("偏好已更新，请刷新后停用最新版本")
    result = add(
        session,
        wid,
        "preference",
        {
            "key": row.data["key"],
            "value": "",
            "confirmed": True,
            "state": "inactive",
            "version": latest.get("version", 1) + 1,
            "expires_at": None,
        },
    )
    audit(session, wid, actor, "preference.deactivated", result.id)
    return view(result)
