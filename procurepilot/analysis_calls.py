"""模型调用的预算预留、独立检查点与用量账本。"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from dataclasses import dataclass

from .model_money import amounts_supported, for_model
from .models import (
    ModelConnectionError,
    ModelResponseError,
    ProviderError,
    REVIEWABLE_RESPONSE_ISSUES,
    request_timeout_seconds,
    required_quantity_checks,
)
from .observability import span
from .service import fail, get
from .storage import add


SPECIALISTS = {"寻源员", "报价分析员", "方案规划员"}
SPLIT_REVIEWS = {
    "寻源员": "独立复核拆单分配涉及的目录规格与来源",
    "报价分析员": "按实际分配逐条复核报价库存和交期",
}


@dataclass(frozen=True)
class ReservedCall:
    id: str
    role: str
    context: dict
    output_limit: int


def _role_context(context, data, role):
    """专业角色各自读取领域快照，核验员才读取其他角色的结论。"""
    role_context = {
        **context,
        "delegated_task": next(
            (task for task in data.get("delegated_tasks", []) if task["role"] == role),
            None,
        ),
        "prior_assessments": data["assessments"] if role == "证据核验员" else [],
    }
    if context.get("plan"):
        plan = context["plan"]
        role_context["plan"] = {
            key: plan[key]
            for key in ("status", "total_cents", "compatibility_evidence")
            if key in plan
        }
        role_context["plan"]["lines"] = [
            {
                key: value
                for key, value in line.items()
                if key
                in ("need_index", "quote_id", "product_id", "quantity", "price_cents")
            }
            for line in plan.get("lines", [])
        ]
    role_context["quotes"] = [
        {key: value for key, value in quote.items() if key != "created_at"}
        for quote in context["quotes"]
    ]
    role_context = for_model(role_context)
    if data.get("catalog_mode") == "on_demand":
        # 快照仍保存目录以重算金额，模型只能引用工具实际返回的目录行。
        role_context["catalog_mode"] = "on_demand"
        role_context["product_evidence"] = []
        role_context["evidence_ids"] = [
            item for item in context["evidence_ids"] if not item.startswith("catalog:")
        ]
        if role == "寻源员":
            # 寻源核对型号与规格，金额和库存交期由报价角色与核验员独立处理。
            role_context["requirement"].pop("budget_cny", None)
            role_context["quotes"] = [
                {"id": quote["id"], "product_id": quote["product_id"]}
                for quote in role_context["quotes"]
            ]
            if role_context.get("plan"):
                role_context["plan"].pop("total_cny", None)
                for line in role_context["plan"].get("lines", []):
                    line.pop("subtotal_cny", None)
    if data.get("tool_results"):
        role_context["tool_results"] = data["tool_results"]
        role_context["evidence_ids"] = list(
            dict.fromkeys(
                [
                    *role_context["evidence_ids"],
                    *[
                        row["id"]
                        for result in data["tool_results"]
                        for row in result.get("output", {}).get("evidence", [])
                    ],
                ]
            )
        )
    return role_context


def _cost(rates, prompt, completion):
    return (
        Decimal(prompt) * Decimal(rates["input"])
        + Decimal(completion) * Decimal(rates["output"])
    ) / Decimal(1_000_000)


def reserve(session, wid, task, roles, context):
    """一次提交整批 pending 账本和累计预算，之后才允许发起请求。"""
    data = dict(task.data)
    output_limit = int(os.getenv("MODEL_MAX_OUTPUT_TOKENS", "2048"))
    if not 512 <= output_limit <= 4096:
        fail("MODEL_MAX_OUTPUT_TOKENS 必须在512至4096之间", 422)
    reserved = []
    tokens = data["tokens"]
    spent = Decimal(data["cost"] or "0")
    limit = Decimal(data["max_cost"]) if data.get("max_cost") is not None else None
    rates = data.get("pricing")
    attempts = data.get("attempts", 0)
    until = datetime.now(timezone.utc) + timedelta(
        seconds=request_timeout_seconds() + 30
    )
    batch_id = None
    for role in roles:
        role_context = _role_context(context, data, role)
        encoded = json.dumps(role_context, ensure_ascii=False)
        estimate = len(encoded) + output_limit
        fee = _cost(rates, estimate, output_limit) if rates else Decimal(0)
        cost_limit = limit is not None and spent + fee > limit
        if (
            data["steps"] + len(reserved) >= data["max_steps"]
            or attempts + len(reserved) >= data["max_steps"] + 3
            or tokens + estimate > data["max_tokens"]
            or datetime.now(timezone.utc)
            > datetime.fromisoformat(data.get("deadline", "2099-01-01T00:00:00+00:00"))
            or cost_limit
        ):
            # 第二个角色若暂时无法预留，等第一个归还预算后再判断。
            if not reserved:
                task.data = {**data, "state": "budget_exhausted"}
                if cost_limit:
                    task.data = {**task.data, "budget_reason": "cost_reservation"}
            break
        call = add(
            session,
            wid,
            "model_call",
            {
                "analysis_id": task.id,
                "role": role,
                "fingerprint": sha256((role + encoded).encode()).hexdigest(),
                "state": "pending",
                "reserved_tokens": estimate,
                "lease_until": until.isoformat(),
                "parallel_batch_id": batch_id,
            },
        )
        if reserved and batch_id is None:
            batch_id = reserved[0].id
            first = get(session, wid, "model_call", batch_id)
            first.data = {**first.data, "parallel_batch_id": batch_id}
            call.data = {**call.data, "parallel_batch_id": batch_id}
        reserved.append(ReservedCall(call.id, role, role_context, output_limit))
        tokens += estimate
        spent += fee
    if reserved:
        data["attempts"] = attempts + len(reserved)
        if batch_id:
            data["parallel_batches"] = [
                *data.get("parallel_batches", []),
                {"id": batch_id, "roles": [call.role for call in reserved]},
            ]
        task.data = data
    return reserved


def _ask(model, task_id, call, context):
    """返回回复或错误，由账本写入端统一区分确定失败和用量未知。"""
    try:
        with span(
            "agent.assess",
            {
                "agent.role": call.role,
                "analysis.id": task_id,
                "langfuse.observation.type": "generation",
                "gen_ai.request.model": os.getenv("MODEL_NAME", "local-adapter"),
            },
        ) as current:
            reply = model.assess(call.role, call.context, call.output_limit)
            current.set_attribute("gen_ai.usage.input_tokens", reply.prompt_tokens)
            current.set_attribute("gen_ai.usage.output_tokens", reply.completion_tokens)
        assessment = reply.assessment
        usage = {
            "prompt_tokens": reply.prompt_tokens,
            "completion_tokens": reply.completion_tokens,
        }
        if not amounts_supported(assessment, context):
            raise ModelResponseError("unsupported_amount", usage)
        coordinator = (
            context.get("coordination_mode") == "dynamic" and call.role == "采购协调员"
        )
        delegated = assessment.delegations
        if delegated and (
            not coordinator or len({task.role for task in delegated}) != len(delegated)
        ):
            raise ModelResponseError(
                "schema_validation", usage, invalid_fields=["delegations"]
            )
        if assessment.tool_requests and not coordinator:
            raise ModelResponseError(
                "schema_validation", usage, invalid_fields=["tool_requests"]
            )
        expected = required_quantity_checks(call.role, call.context)
        if expected:
            if assessment.quantity_checks is None:
                raise ModelResponseError(
                    "schema_validation", usage, invalid_fields=["quantity_checks"]
                )
            checked = sorted(
                (row.model_dump() for row in assessment.quantity_checks),
                key=lambda row: row["need_index"],
            )
            if checked != expected:
                raise ModelResponseError(
                    "quantity_mismatch", usage, invalid_fields=["quantity_checks"]
                )
        elif assessment.quantity_checks is not None:
            raise ModelResponseError(
                "schema_validation", usage, invalid_fields=["quantity_checks"]
            )
        if set(assessment.evidence_ids) - set(call.context["evidence_ids"]):
            raise ModelResponseError(
                "unknown_evidence", usage, invalid_fields=["evidence_ids"]
            )
        return reply, None
    except Exception as error:
        return None, error


def perform(model, task_id, calls, context):
    """两个独立角色并发执行，每个响应到达后立即交还调度器持久化。"""
    if len(calls) == 1:
        yield calls[0], _ask(model, task_id, calls[0], context)
        return
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = {
            pool.submit(copy_context().run, _ask, model, task_id, call, context): call
            for call in calls
        }
        for future in as_completed(futures):
            yield futures[future], future.result()


def _failure(data, ledger, error):
    rejected = isinstance(error, ProviderError) and error.definitely_rejected
    diagnostic = {}
    usage = None
    state = "tool_unavailable"
    if isinstance(error, ProviderError):
        diagnostic = {
            "http_status": error.status_code,
            "provider_code": error.provider_code,
            "hint": error.hint,
        }
        if error.status_code == 429 and error.provider_code in ("1302", "1305"):
            state = "waiting_provider"
    if isinstance(error, (ModelResponseError, ModelConnectionError)):
        diagnostic = error.diagnostic
    if isinstance(error, ModelResponseError):
        usage = error.usage
        if (
            usage is not None
            and diagnostic.get("response_issue") in REVIEWABLE_RESPONSE_ISSUES
        ):
            state = "waiting_response"
    uncertain = not rejected and usage is None
    ledger.data = {
        **ledger.data,
        **diagnostic,
        "state": "invalid_response"
        if isinstance(error, ModelResponseError)
        else "rejected"
        if rejected
        else "uncertain",
        "error_type": type(error).__name__,
        "usage": usage,
    }
    # 一条未知结果就阻止整个任务重发，后到的已知错误不能覆盖它。
    priority = {
        "running": 0,
        "waiting_response": 1,
        "waiting_provider": 2,
        "tool_unavailable": 3,
        "budget_exhausted": 4,
        "cancelled": 5,
    }
    if not data.get("uncertain_usage") and (
        uncertain or priority[state] >= priority[data["state"]]
    ):
        if data["state"] != "cancelled":
            data["state"] = state
        data["error"] = diagnostic or {
            "hint": "模型调用未完成，请检查连接、响应格式或供应商记录"
        }
        if state == "waiting_provider":
            data["retry_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=30)
            ).isoformat()
            data["retry_mode"] = "manual"
    data["uncertain_usage"] = bool(data.get("uncertain_usage") or uncertain)
    return usage


def settle(session, wid, task, worker_token, call, outcome):
    """提交单个已发请求的真实用量，包括取消期间到达的并发结果。"""
    ledger = get(session, wid, "model_call", call.id)
    if ledger.data["state"] != "pending" or (
        worker_token is not None and task.data.get("worker_token") != worker_token
    ):
        return
    data = dict(task.data)
    reply, error = outcome
    if error is not None:
        usage = _failure(data, ledger, error)
    else:
        usage = {
            "prompt_tokens": reply.prompt_tokens,
            "completion_tokens": reply.completion_tokens,
        }
        ledger.data = {**ledger.data, "state": "completed", "usage": usage}
        assessment = reply.assessment
        data["steps"] += 1
        if call.role == "采购协调员" and data.get("coordination_mode") == "dynamic":
            selected = assessment.delegations
            tasks = {item.role: item.model_dump() for item in selected or []}
            added = []
            if call.context.get("requires_split_review"):
                for role, objective in SPLIT_REVIEWS.items():
                    if role not in tasks:
                        tasks[role] = {"role": role, "objective": objective}
                        added.append(role)
                # 将两个独立复核放在相邻位置，正常预算下可并行执行。
                tasks = {**{role: tasks[role] for role in SPLIT_REVIEWS}, **tasks}
                if added:
                    data["policy_added_roles"] = added
            data["roles"] = (
                data["fallback_roles"]
                if selected is None and not added
                else ["采购协调员", *tasks, "证据核验员"]
            )
            data["delegated_tasks"] = list(tasks.values())
            data["delegation_state"] = (
                "policy_completed"
                if added
                else "fallback"
                if selected is None
                else "model_selected"
            )
            if assessment.tool_requests:
                data["tool_requests"] = [
                    item.model_dump() for item in assessment.tool_requests
                ]
        assessment_data = assessment.model_dump(
            exclude={"quantity_checks"} if assessment.quantity_checks is None else None
        )
        assessments = [
            *data["assessments"],
            {"role": call.role, **assessment_data},
        ]
        order = data.get("roles", ["采购协调员", "寻源与报价分析员", "证据核验员"])
        data["assessments"] = sorted(
            assessments, key=lambda item: order.index(item["role"])
        )
    if usage is not None:
        data["tokens"] += sum(usage.values())
        rates = data.get("pricing")
        exceeds_cost = False
        if rates:
            spent = Decimal(data["cost"]) + _cost(
                rates, usage["prompt_tokens"], usage["completion_tokens"]
            )
            data["cost"] = str(spent)
            exceeds_cost = data.get("max_cost") is not None and spent > Decimal(
                data["max_cost"]
            )
        if data["state"] != "cancelled" and not data.get("uncertain_usage"):
            if exceeds_cost or data["tokens"] > data["max_tokens"]:
                data["state"] = "budget_exhausted"
                if exceeds_cost:
                    data["budget_reason"] = "reported_cost"
    task.data = data
