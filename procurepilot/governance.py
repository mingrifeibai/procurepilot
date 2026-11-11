"""只读工具治理：按工作区、身份、输入和依赖版本复用，限制重复无进展调用。"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from .storage import add, records
from .service import fail


def read(
    session, wid, actor, run_id, name, arguments, dependencies, loader, ttl_seconds=300
):
    """仅供可信后端注册的只读工具调用；写操作禁止使用缓存。

    依赖快照由调用方从数据库读取，模型不能提供版本标签。不同工作区或
    身份不共享结果。三个相同指纹且无新依赖的连续请求触发无进展停止。
    """
    payload = {
        "actor": actor,
        "name": name,
        "arguments": arguments,
        "dependencies": dependencies,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()
    calls = records(session, wid, "tool_call")
    recent = [c for c in calls if c.data["run_id"] == run_id][-2:]
    if len(recent) == 2 and all(c.data["fingerprint"] == fingerprint for c in recent):
        fail("工具连续调用无新进展，已停止重复执行", 422)
    now = datetime.now(timezone.utc)
    cache = next(
        (
            c
            for c in reversed(calls)
            if c.data["fingerprint"] == fingerprint
            and datetime.fromisoformat(c.data["expires_at"]) > now
        ),
        None,
    )
    result = cache.data["result"] if cache else loader()
    if len(json.dumps(result, ensure_ascii=False)) > 100000:
        fail("工具结果超过预览上限，请外置原文后分页读取", 422)
    add(
        session,
        wid,
        "tool_call",
        {
            "run_id": run_id,
            "tool": name,
            "actor": actor,
            "fingerprint": fingerprint,
            "cache_hit": cache is not None,
            "result": result,
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
        },
    )
    return result, cache is not None
