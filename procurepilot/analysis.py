"""多角色分析 Harness。持久化调用指纹、共享用量、有限步骤与故障状态。

这是只读分析运行，与确定性的采购执行分离。远端响应丢失时不能证明
未计费，因此标记 uncertain 并停止，不自动重复发起可能已付费的请求。
"""

import json
from collections import defaultdict
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from . import analysis_calls, clarification, memory
from .jobs import check_owner, public
from .models import ChatCompletionsModel, REVIEWABLE_RESPONSE_ISSUES
from .service import CATALOG, fail, get, latest
from .storage import add, records, view


ROLES = ("采购协调员", "寻源与报价分析员", "证据核验员")


def run(
    store,
    wid,
    actor,
    model=None,
    max_tokens=16000,
    max_steps=5,
    task_id=None,
    stop_after=None,
    pricing=None,
    max_cost=None,
    max_seconds=120,
    prepare_only=False,
    worker_token=None,
    idempotency_key=None,
    coordination_mode="fixed",
    catalog_mode="preloaded",
    context_excerpt="focused",
):
    # 入队仅固化输入和预算，不建立模型连接；密钥始终由执行进程读取。
    if task_id is None:
        if coordination_mode not in ("fixed", "dynamic", "single"):
            fail("未知分析编排模式", 422)
        if context_excerpt not in ("focused", "full_chunk"):
            fail("未知附件上下文策略", 422)
        if catalog_mode not in ("preloaded", "on_demand") or (
            catalog_mode == "on_demand" and coordination_mode != "dynamic"
        ):
            fail("目录按需取证需要动态委派模式", 422)

        if max_cost is not None and pricing is None:
            fail("费用预算需要配置输入与输出的每百万 Token 单价", 422)
        if pricing is not None:
            try:
                rates = [Decimal(str(pricing[k])) for k in ("input", "output")]
                if any(not r.is_finite() or r < 0 for r in rates):
                    raise ValueError()
                pricing = {k: str(v) for k, v in zip(("input", "output"), rates)}
            except (ValueError, KeyError, ArithmeticError):
                fail("模型计价配置无效", 422)
        if max_cost is not None and (
            not Decimal(str(max_cost)).is_finite() or Decimal(str(max_cost)) <= 0
        ):
            fail("费用上限必须为正数", 422)
        with store.transaction(wid) as s:
            if prepare_only:
                queued = records(s, wid, "analysis")
                for prior in queued:
                    if (
                        idempotency_key
                        and prior.data.get("idempotency_key") == idempotency_key
                    ):
                        if any(
                            prior.data.get(
                                k,
                                {
                                    "coordination_mode": "fixed",
                                    "catalog_mode": "preloaded",
                                    "context_excerpt": "focused",
                                }.get(k),
                            )
                            != v
                            for k, v in {
                                "actor": actor,
                                "max_tokens": max_tokens,
                                "max_steps": max_steps,
                                "coordination_mode": coordination_mode,
                                "catalog_mode": catalog_mode,
                                "context_excerpt": context_excerpt,
                            }.items()
                        ):
                            fail("同一任务操作键不能用于不同提交参数", 409)
                        return public(prior)
                if (
                    sum(
                        r.data.get("dispatch_state") in ("queued", "leased")
                        for r in queued
                    )
                    >= 20
                ):
                    fail("当前工作区后台队列已满，请等待或取消已有任务", 429)
            requirement = latest(s, wid, "requirement")
            quotes = [view(q) for q in records(s, wid, "quote")]
            plans = records(s, wid, "plan")
            artifacts = records(s, wid, "artifact")
            from .retrieval import focus_excerpt, search as retrieve, requirement_query

            query = requirement_query(requirement.data)
            artifact_hits = (
                retrieve(
                    [view(a) for a in artifacts],
                    query,
                    limit=4,
                )
                if artifacts
                else []
            )
            from .settings import search_backend

            if artifacts and search_backend() == "opensearch":
                from .indexing import search as indexed_search

                artifact_hits = indexed_search(s, wid, query, 4)
            previews = []
            for hit in artifact_hits:
                excerpt = (
                    focus_excerpt(hit, query)
                    if context_excerpt == "focused"
                    else {"preview": hit["snippet"], "preview_offset": hit["offset"]}
                )
                offset = excerpt["preview_offset"]
                previews.append(
                    {
                        "id": f"artifact:{hit['artifact_id']}:offset:{offset}",
                        "artifact_id": hit["artifact_id"],
                        "offset": offset,
                        "preview": excerpt["preview"],
                        "untrusted": True,
                    }
                )
            # 报价只能支持价格事实，接口和内存等产品事实必须显式提供目录证据。
            product_evidence = [
                {
                    "id": "catalog:" + pid,
                    "product": CATALOG[pid],
                    "source": CATALOG[pid]["source"],
                    "simulated": True,
                }
                for pid in sorted({q["product_id"] for q in quotes})
                if pid in CATALOG
            ]
            preferences = memory.current(s, wid)
            answers = clarification.confirmed_answers(s, wid, requirement.id)
            context = {
                "coordination_mode": coordination_mode,
                "requirement": requirement.data,
                "quotes": quotes,
                "plan": plans[-1].data
                if plans and plans[-1].data["requirement_id"] == requirement.id
                else None,
                "evidence_ids": [q["id"] for q in quotes]
                + [row["id"] for row in previews]
                + [p["id"] for p in product_evidence],
                "product_evidence": product_evidence,
                "preferences": preferences,
                "confirmed_answers": answers,
                "artifacts": previews,
                "retrieval": {
                    "method": artifact_hits[0]["method"]
                    if artifact_hits
                    else search_backend(),
                    "selected_chunks": len(artifact_hits),
                    "total_artifacts": len(artifacts),
                    "context_excerpt": context_excerpt,
                },
                "artifact_manifest": [a.id for a in artifacts],
            }
            if coordination_mode == "dynamic":
                # 同一需求由多条报价共同供货时，分别复核规格和履约条件。
                quotes_by_need = defaultdict(set)
                plan = context["plan"] or {}
                if plan.get("status") == "feasible":
                    for line in plan.get("lines", []):
                        quotes_by_need[line["need_index"]].add(line["quote_id"])
                if any(len(quote_ids) > 1 for quote_ids in quotes_by_need.values()):
                    context["requires_split_review"] = True
            context["evidence_ids"] += [
                "preference:" + p["id"] for p in preferences
            ] + [a["id"] for a in answers]
            # 字符代理故意保守；实际用量从供应商返回，保留二者以便评测误差。
            serialized = json.dumps(context, ensure_ascii=False)
            if len(serialized) > 12000:
                fail("分析上下文超过上限，请缩小候选与证据范围", 422)
            base_roles = (
                list(ROLES)
                if len(requirement.data["needs"]) == 1 and not artifacts
                else ["采购协调员", "寻源员", "报价分析员", "方案规划员", "证据核验员"]
            )
            initial_roles = {
                "fixed": base_roles,
                "dynamic": ["采购协调员", "证据核验员"],
                "single": ["采购协调员"],
            }
            task = add(
                s,
                wid,
                "analysis",
                {
                    "actor": actor,
                    "state": "queued" if prepare_only else "running",
                    "dispatch_state": "queued" if prepare_only else "direct",
                    "idempotency_key": idempotency_key,
                    "coordination_mode": coordination_mode,
                    "catalog_mode": catalog_mode,
                    "context_excerpt": context_excerpt,
                    "fallback_roles": base_roles,
                    "evidence_ranges": {
                        row["id"]: {
                            "offset": row["offset"],
                            "end_offset": row["offset"] + len(row["preview"]),
                            "bytes": len(row["preview"].encode("utf-8")),
                        }
                        for row in previews
                    },
                    "requirement_id": requirement.id,
                    "steps": 0,
                    "tokens": 0,
                    "max_tokens": max_tokens,
                    "max_steps": max_steps,
                    "assessments": [],
                    "accounting": "provider_reported_tokens",
                    "cost": "0" if pricing else None,
                    "pricing": pricing,
                    "max_cost": str(max_cost) if max_cost is not None else None,
                    "deadline": (
                        datetime.now(timezone.utc) + timedelta(seconds=max_seconds)
                    ).isoformat(),
                    "context": context,
                    "roles": initial_roles[coordination_mode],
                },
            )
            task_id = task.id
            if prepare_only:
                from .streams import schedule

                schedule(s, wid, task_id)
                return public(task)
    model = model or ChatCompletionsModel()
    with store.transaction(wid) as s:
        task = get(s, wid, "analysis", task_id)
        check_owner(task, worker_token)
        # 只有已知截断且用量完整的响应允许显式恢复；未知远端结果保持终止。
        if (
            task.data["state"] in ("tool_unavailable", "waiting_response")
            and (task.data.get("error") or {}).get("response_issue")
            in REVIEWABLE_RESPONSE_ISSUES
            and task.data.get("uncertain_usage") is False
        ):
            task.data = {**task.data, "state": "running"}
        if task.data["state"] in (
            "completed",
            "evidence_insufficient",
            "budget_exhausted",
            "stale",
            "cancelled",
            "tool_unavailable",
        ):
            return public(task)
        pending = [
            c
            for c in records(s, wid, "model_call")
            if c.data["analysis_id"] == task_id and c.data["state"] == "pending"
        ]
        if pending:
            if any(
                datetime.fromisoformat(
                    c.data.get("lease_until", "2099-01-01T00:00:00+00:00")
                )
                > datetime.now(timezone.utc)
                for c in pending
            ):
                fail("分析调用仍在运行，请稍后查询")
            for call in pending:
                call.data = {
                    **call.data,
                    "state": "uncertain",
                    "error_type": "ProcessInterrupted",
                }
            task.data = {
                **task.data,
                "state": "cancelled"
                if task.data["state"] == "cancelled"
                else "tool_unavailable",
                "uncertain_usage": True,
            }
            return public(task)
        context = task.data["context"]
        if task.data["state"] == "waiting_provider":
            if datetime.now(timezone.utc) < datetime.fromisoformat(
                task.data["retry_at"]
            ):
                return public(task)
            task.data = {**task.data, "state": "running"}
    while True:
        with store.transaction(wid) as s:
            task = get(s, wid, "analysis", task_id)
            check_owner(task, worker_token, renew=True)
            data = task.data
            if data["state"] != "running":
                break
            completed = {item["role"] for item in data["assessments"]}
            remaining = [
                role for role in data.get("roles", ROLES) if role not in completed
            ]
            if not remaining:
                break
            if any(
                row.data["analysis_id"] == task_id and row.data["state"] == "pending"
                for row in records(s, wid, "model_call")
            ):
                fail("已有执行者持有当前分析调用")
            if _stale(s, wid, data, context):
                task.data = {**data, "state": "stale"}
                break
            requests = data.get("tool_requests") if "tool_results" not in data else None
            if requests:
                if (
                    datetime.now(timezone.utc)
                    > datetime.fromisoformat(data["deadline"])
                    or data["steps"] >= data["max_steps"]
                ):
                    task.data = {**data, "state": "budget_exhausted"}
                    break
                calls = []
            else:
                parallel = (
                    stop_after is None
                    and data.get("delegation_state")
                    in ("model_selected", "policy_completed")
                    and len(remaining) > 1
                    and set(remaining[:2]) <= analysis_calls.SPECIALISTS
                )
                calls = analysis_calls.reserve(
                    s, wid, task, remaining[:2] if parallel else remaining[:1], context
                )
        if requests:
            _collect_tools(store, wid, task_id, worker_token, context, requests)
            continue
        if not calls:
            break
        for call, outcome in analysis_calls.perform(model, task_id, calls, context):
            with store.transaction(wid) as s:
                task = get(s, wid, "analysis", task_id)
                analysis_calls.settle(s, wid, task, worker_token, call, outcome)
            if call.role == stop_after and outcome[1] is None:
                raise InterruptedError("故障注入：角色完成后中断")
    with store.transaction(wid) as s:
        task = get(s, wid, "analysis", task_id)
        check_owner(task, worker_token)
        assessments = task.data["assessments"]
        if task.data.get("coordination_mode") == "dynamic":
            # 取证前的缺口保留在轨迹中，由已看到所有结果的核验员确认哪些仍未解决。
            assessments = [
                row for row in assessments if row["role"] == "证据核验员"
            ] or assessments
        if task.data["state"] == "running":
            results = task.data.get("tool_results", [])
            catalog_ids = {
                row["id"]
                for result in results
                if result["tool"] == "search_products"
                for row in result.get("output", {}).get("evidence", [])
            }
            catalog_gap = task.data.get("catalog_mode") == "on_demand" and not any(
                item["role"] == "证据核验员"
                and catalog_ids.intersection(item["evidence_ids"])
                for item in task.data["assessments"]
            )
            state = (
                "stale"
                if _stale(s, wid, task.data, context)
                else "tool_unavailable"
                if any(result["state"] == "unavailable" for result in results)
                else "evidence_insufficient"
                if catalog_gap
                or any(item["missing_information"] for item in assessments)
                else "completed"
            )
            task.data = {**task.data, "state": state}
            if catalog_gap and state == "evidence_insufficient":
                task.data = {
                    **task.data,
                    "evidence_gap": "本次按需分析缺少核验员引用的目录工具证据",
                }
            if state == "tool_unavailable":
                task.data = {
                    **task.data,
                    "uncertain_usage": False,
                    "error": {"hint": "部分只读工具未返回证据，请查看工具结果"},
                }
        if task.data["state"] == "evidence_insufficient":
            clarification.create_questions(s, wid, task, assessments)
        if task.data["state"] in ("completed", "evidence_insufficient"):
            task.data = {
                **task.data,
                "error": None,
                "retry_at": None,
                "uncertain_usage": False,
            }
        task.data = {
            **task.data,
            "elapsed_ms": round(
                (
                    datetime.now(timezone.utc) - datetime.fromisoformat(task.created_at)
                ).total_seconds()
                * 1000
            ),
        }
        return public(task)


def _stale(session, wid, data, context):
    return (
        latest(session, wid, "requirement").id != data["requirement_id"]
        or [view(row) for row in records(session, wid, "quote")] != context["quotes"]
        or memory.current(session, wid) != context.get("preferences", [])
        or clarification.confirmed_answers(session, wid, data["requirement_id"])
        != context.get("confirmed_answers", [])
        or (
            "artifact_manifest" in context
            and [row.id for row in records(session, wid, "artifact")]
            != context["artifact_manifest"]
        )
    )


def _collect_tools(store, wid, task_id, worker_token, context, requests):
    # 读工具可以安全重试，只有首次提交的结果进入模型上下文和审计。
    from . import analysis_tools, governance

    results = analysis_tools.execute(
        store, wid, context.get("artifact_manifest", []), requests
    )
    with store.transaction(wid) as session:
        task = get(session, wid, "analysis", task_id)
        check_owner(task, worker_token)
        data = task.data
        if data["state"] != "running" or "tool_results" in data:
            return
        if _stale(session, wid, data, context):
            task.data = {**data, "state": "stale"}
            return
        for result in results:
            governance.read(
                session,
                wid,
                data["actor"],
                task_id,
                result["tool"],
                result["arguments"],
                {"analysis": task_id},
                lambda: result,
                ttl_seconds=0,
            )
        task.data = {**data, "tool_results": results}
