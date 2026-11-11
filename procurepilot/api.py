"""HTTP 接口与服务端权限边界。所有文件和工具内容只作为数据处理。"""

import csv
import base64
import io
import json
import os
from contextlib import asynccontextmanager
from threading import Event, Thread
from pathlib import Path
from uuid import uuid4
from typing import Annotated, Literal
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from sqlalchemy import select
from .domain import StrictModel, WorkspaceInput, Requirement, QuoteInput, ReceiptInput
from .storage import Store, Workspace, Organization, OrganizationMember, records, view
from . import service, runtime
from . import evidence
from . import analysis
from . import clarification
from . import organizations
from .domain import ResolutionInput
from .members import MemberInput


class Execution(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=100)


class RunInput(StrictModel):
    max_steps: int = Field(default=8, ge=1, le=100)


class ReplanInput(StrictModel):
    previous_plan_id: str = Field(min_length=1, max_length=36)


class ContinuationInput(StrictModel):
    seconds: int = Field(default=300, ge=30, le=300)


class AnalysisOptions(StrictModel):
    max_tokens: int = Field(default=16000, ge=1000, le=64000)
    max_steps: int = Field(default=5, ge=1, le=10)
    coordination_mode: Literal["fixed", "dynamic", "single"] = "fixed"
    catalog_mode: Literal["preloaded", "on_demand"] = "preloaded"
    context_excerpt: Literal["focused", "full_chunk"] = "focused"


class AnalysisJobInput(AnalysisOptions):
    idempotency_key: str = Field(min_length=8, max_length=100)


def create_app(store=None):
    from .observability import configure, span, trace_id

    configure()
    db = store or Store()
    worker_mode = os.getenv("MODEL_WORKER_MODE", "embedded")
    if worker_mode not in ("embedded", "external", "disabled"):
        raise RuntimeError("MODEL_WORKER_MODE 必须是 embedded、external 或 disabled")

    @asynccontextmanager
    async def lifespan(app):
        """开发环境自动消费已确认的队列；生产可以单独运行 Worker。"""
        import asyncio
        from .worker import serve

        stop = Event()
        thread = None
        if worker_mode == "embedded":
            thread = Thread(
                target=serve, args=(db, stop), daemon=True, name="analysis-worker"
            )
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread:
                await asyncio.to_thread(thread.join, 5)

    app = FastAPI(title="ProcurePilot", version="1.0.0-rc.2", lifespan=lifespan)

    @app.middleware("http")
    async def trace_requests(request, call_next):
        with span("http.request", {"http.request.method": request.method}) as current:
            response = await call_next(request)
            route = getattr(request.scope.get("route"), "path", "static")
            current.update_name(request.method + " " + route)
            current.set_attribute("http.response.status_code", response.status_code)
            identifier = trace_id()
            if identifier:
                response.headers["X-Trace-Id"] = identifier
            return response

    demo = os.getenv("DEMO_MODE", "1") == "1"
    tokens = json.loads(os.getenv("API_TOKENS_JSON", "{}"))
    if demo:
        tokens.update(
            {
                "demo-buyer": {"role": "buyer", "workspaces": ["*"]},
                "demo-approver": {"role": "approver", "workspaces": ["*"]},
                "demo-viewer": {"role": "viewer", "workspaces": ["*"]},
            }
        )
    if not tokens:
        raise RuntimeError("非演示模式必须配置 API_TOKENS_JSON")

    def identity(authorization: Annotated[str | None, Header()] = None):
        token = (authorization or "").removeprefix("Bearer ")
        if token in tokens:
            return {
                **tokens[token],
                "actor": tokens[token].get("name", tokens[token]["role"]),
            }
        if token.startswith(("ppw_", "pporg_")):
            from sqlalchemy.orm import Session
            from .members import authenticate

            with Session(db.engine) as s:
                member = (
                    organizations.authenticate(s, token)
                    if token.startswith("pporg_")
                    else authenticate(s, token)
                )
            if member:
                return member
        raise HTTPException(401, "请提供有效的访问令牌")

    def access(wid, user, write=False, approval=False):
        if wid not in user.get("workspaces", []) and "*" not in user.get(
            "workspaces", []
        ):
            raise HTTPException(403, "无此工作区权限")
        if write and user["role"] not in ("buyer", "approver", "admin"):
            raise HTTPException(403, "只读角色不能修改数据")
        if approval and user["role"] not in ("approver", "admin"):
            raise HTTPException(403, "需要审批角色")
        with db.transaction(wid) as s:
            if (
                s.scalar(organizations.workspace_scope(user).where(Workspace.id == wid))
                is None
            ):
                raise HTTPException(404, "工作区不存在")

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "mode": "demo" if demo else "configured",
            "supplier": "simulated",
        }

    @app.get("/api/session")
    def session_info(user=Depends(identity)):
        return {
            "role": user["role"],
            "name": user.get("name", user["role"]),
            "demo": demo,
            "organization_id": user.get("organization_id"),
            "platform_admin": organizations.platform_admin(user),
        }

    @app.get("/api/organizations")
    def list_organizations(user=Depends(identity)):
        query = select(Organization).order_by(Organization.name, Organization.id)
        if not organizations.platform_admin(user):
            query = query.where(Organization.id == user.get("organization_id"))
        with db.transaction() as s:
            return [organizations.public(row) for row in s.scalars(query)]

    @app.post("/api/organizations", status_code=201)
    def create_organization(
        body: organizations.OrganizationInput, user=Depends(identity)
    ):
        if not organizations.platform_admin(user):
            raise HTTPException(403, "创建组织需要平台管理员权限")
        with db.transaction() as s:
            row = Organization(id=str(uuid4()), name=body.name.strip())
            s.add(row)
            return organizations.public(row)

    @app.get("/api/organizations/{oid}/members")
    def list_organization_members(oid: str, user=Depends(identity)):
        with db.transaction() as s:
            organizations.authorize_admin(s, oid, user)
            return [
                organizations.member_public(row)
                for row in s.scalars(
                    select(OrganizationMember)
                    .where(OrganizationMember.organization_id == oid)
                    .order_by(OrganizationMember.created_at, OrganizationMember.id)
                )
            ]

    @app.post("/api/organizations/{oid}/members", status_code=201)
    def issue_organization_member(
        oid: str,
        body: organizations.OrganizationMemberInput,
        response: Response,
        user=Depends(identity),
    ):
        with db.transaction() as s:
            member = organizations.issue(s, oid, user, body)
            response.headers["Cache-Control"] = "no-store"
            return member

    @app.delete("/api/organizations/{oid}/members/{mid}")
    def revoke_organization_member(oid: str, mid: str, user=Depends(identity)):
        with db.transaction() as s:
            return organizations.revoke(s, oid, mid, user)

    def member_admin(wid, user):
        if user["role"] != "admin":
            raise HTTPException(403, "需要管理员权限")
        access(wid, user)

    @app.get("/api/w/{wid}/members")
    def list_members(wid: str, user=Depends(identity)):
        from .members import public

        member_admin(wid, user)
        with db.transaction(wid) as s:
            return [public(member) for member in records(s, wid, "member")]

    @app.post("/api/w/{wid}/members", status_code=201)
    def issue_member(
        wid: str, body: MemberInput, response: Response, user=Depends(identity)
    ):
        from .members import issue

        member_admin(wid, user)
        response.headers["Cache-Control"] = "no-store"
        with db.transaction(wid) as s:
            return issue(s, wid, user["actor"], body)

    @app.delete("/api/w/{wid}/members/{mid}")
    def revoke_member(wid: str, mid: str, user=Depends(identity)):
        from .members import revoke

        member_admin(wid, user)
        with db.transaction(wid) as s:
            return revoke(s, wid, user["actor"], mid)

    @app.get("/api/workspaces")
    def workspaces(user=Depends(identity)):
        with db.transaction() as s:
            return [
                service.workspace_view(w)
                for w in s.scalars(organizations.workspace_scope(user))
            ]

    @app.post("/api/workspaces", status_code=201)
    def workspace(body: WorkspaceInput, user=Depends(identity)):
        if user["role"] not in ("buyer", "admin") or "*" not in user.get(
            "workspaces", []
        ):
            raise HTTPException(403, "创建工作区需要组织级采购权限")
        scope = user.get("organization_id")
        if (
            body.organization_id
            and body.organization_id != scope
            and not organizations.platform_admin(user)
        ):
            raise HTTPException(403, "不能在其他组织创建工作区")
        oid = body.organization_id or scope
        with db.transaction() as s:
            if oid and s.get(Organization, oid) is None:
                raise HTTPException(404, "组织不存在")
            return service.create_workspace(s, body.name, user["actor"], oid)

    @app.get("/api/catalog")
    def catalog(user=Depends(identity)):
        return service.PRODUCTS

    @app.get("/api/compatibility")
    def compatibility(user=Depends(identity)):
        from .graph import relations

        return relations()

    @app.get("/api/w/{wid}/snapshot")
    def snapshot(wid: str, user=Depends(identity)):
        access(wid, user)
        with db.transaction(wid) as s:
            return {
                kind: [
                    {
                        k: v
                        for k, v in view(r).items()
                        if not (kind == "analysis" and k in ("context", "worker_token"))
                        and not (kind == "index_job" and k == "lease_token")
                    }
                    for r in records(s, wid, kind)
                ]
                for kind in (
                    "requirement",
                    "quote",
                    "plan",
                    "approval",
                    "order",
                    "receipt",
                    "exception",
                    "run",
                    "trace",
                    "audit",
                    "outbox",
                    "notification",
                    "analysis",
                    "model_call",
                    "tool_call",
                    "queue_outbox",
                    "index_job",
                )
            }

    @app.post("/api/w/{wid}/attachments", status_code=201)
    def attachment(wid: str, body: evidence.Attachment, user=Depends(identity)):
        access(wid, user, write=True)
        extracted = evidence.extract(body)
        from .objects import object_store

        extracted["object"] = object_store().put(
            base64.b64decode(body.content_base64, validate=True)
        )
        with db.transaction(wid) as s:
            from .storage import add

            item = add(s, wid, "artifact", extracted)
            service.audit(s, wid, user["actor"], "artifact.uploaded", item.id)
            return {
                "id": item.id,
                "filename": body.filename,
                "characters": extracted["characters"],
                "preview": extracted["text"][:1000],
                "untrusted": True,
            }

    @app.get("/api/w/{wid}/attachments")
    def attachments(wid: str, user=Depends(identity)):
        access(wid, user)
        with db.transaction(wid) as s:
            return [
                {k: v for k, v in view(a).items() if k != "text"}
                for a in records(s, wid, "artifact")
            ]

    @app.get("/api/w/{wid}/attachments/{aid}")
    def attachment_read(
        wid: str, aid: str, offset: int = 0, limit: int = 2000, user=Depends(identity)
    ):
        access(wid, user)
        if offset < 0 or not 1 <= limit <= 10000:
            raise HTTPException(422, "offset >= 0，limit 介于 1 到 10000")
        with db.transaction(wid) as s:
            a = service.get(s, wid, "artifact", aid)
            return {
                "id": aid,
                "offset": offset,
                "text": a.data["text"][offset : offset + limit],
                "total": a.data["characters"],
                "untrusted": True,
            }

    @app.get("/api/w/{wid}/attachments/{aid}/quotes-preview")
    def preview(wid: str, aid: str, user=Depends(identity)):
        access(wid, user)
        with db.transaction(wid) as s:
            a = service.get(s, wid, "artifact", aid)
            if not a.data["filename"].lower().endswith(".csv"):
                raise HTTPException(422, "请使用CSV报价文件")
            return evidence.quote_preview(a.data["text"], aid)

    @app.post("/api/w/{wid}/attachments/{aid}/quotes-import")
    def import_quotes(
        wid: str, aid: str, body: evidence.QuoteImport, user=Depends(identity)
    ):
        access(wid, user, write=True)
        with db.transaction(wid) as s:
            artifact = service.get(s, wid, "artifact", aid)
            if not artifact.data["filename"].lower().endswith(".csv"):
                raise HTTPException(422, "请使用CSV报价文件")
            return evidence.import_quotes(s, wid, artifact, body.rows, user["actor"])

    @app.get("/api/w/{wid}/attachments/{aid}/original")
    def attachment_original(wid: str, aid: str, user=Depends(identity)):
        access(wid, user)
        from .objects import object_store
        from urllib.parse import quote

        with db.transaction(wid) as s:
            artifact = service.get(s, wid, "artifact", aid)
            if not artifact.data.get("object"):
                raise HTTPException(409, "旧附件尚未保存原件，请重新上传")
            metadata = artifact.data["object"]
            filename = artifact.data["filename"].replace("\\", "/").split("/")[-1]
        content = object_store(metadata.get("backend", "local")).get(metadata)
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(filename, safe=""),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/w/{wid}/evidence/search")
    def search(wid: str, query: str, limit: int = 20, user=Depends(identity)):
        access(wid, user)
        from .retrieval import search as retrieve

        with db.transaction(wid) as s:
            from .settings import search_backend

            if search_backend() == "opensearch":
                from .indexing import search as indexed_search

                return indexed_search(s, wid, query, limit)
            return retrieve(
                [view(a) for a in records(s, wid, "artifact")], query, limit
            )

    @app.post("/api/w/{wid}/evidence/index", status_code=202)
    def build_evidence_index(wid: str, user=Depends(identity)):
        access(wid, user, write=True)
        if worker_mode == "disabled":
            raise HTTPException(503, "后台执行已禁用")
        from .indexing import enqueue

        with db.transaction(wid) as s:
            result = enqueue(s, wid, user["actor"])
            return {k: v for k, v in result.items() if k != "lease_token"}

    @app.get("/api/w/{wid}/evidence/index")
    def evidence_index_status(wid: str, user=Depends(identity)):
        access(wid, user)
        from .search_index import generation

        with db.transaction(wid) as s:
            revision = generation([view(a) for a in records(s, wid, "artifact")])
            return [
                {
                    **{k: v for k, v in view(j).items() if k != "lease_token"},
                    "stale": j.data["generation"] != revision,
                }
                for j in records(s, wid, "index_job")
            ]

    @app.post("/api/w/{wid}/preferences", status_code=201)
    def preference(wid: str, body: evidence.Preference, user=Depends(identity)):
        access(wid, user, write=True)
        if not body.confirmed:
            raise HTTPException(422, "长期偏好必须明确确认")
        with db.transaction(wid) as s:
            from .memory import save

            return save(s, wid, body, user["actor"])

    @app.get("/api/w/{wid}/preferences")
    def preferences(wid: str, user=Depends(identity)):
        access(wid, user)
        from .memory import current

        with db.transaction(wid) as s:
            return current(s, wid, True)

    @app.delete("/api/w/{wid}/preferences/{pid}")
    def preference_deactivate(wid: str, pid: str, user=Depends(identity)):
        access(wid, user, write=True)
        from .memory import deactivate

        with db.transaction(wid) as s:
            return deactivate(s, wid, pid, user["actor"])

    @app.get("/api/w/{wid}/clarifications")
    def clarifications(wid: str, user=Depends(identity)):
        access(wid, user)
        with db.transaction(wid) as s:
            requirements = records(s, wid, "requirement")
            if not requirements:
                return []
            latest = requirements[-1].id
            return [
                {
                    **view(r),
                    "stale": r.data.get(
                        "resolved_requirement_id", r.data["requirement_id"]
                    )
                    != latest,
                }
                for r in records(s, wid, "clarification")
            ]

    @app.post("/api/w/{wid}/clarifications/{qid}/answer")
    def answer_clarification(
        wid: str, qid: str, body: clarification.Answer, user=Depends(identity)
    ):
        access(wid, user, write=True)
        with db.transaction(wid) as s:
            return clarification.answer(s, wid, qid, body, user["actor"])

    @app.get("/api/w/{wid}/context")
    def context(wid: str, user=Depends(identity)):
        access(wid, user)
        with db.transaction(wid) as s:
            return evidence.context_packet(
                service.latest(s, wid, "requirement").data,
                [view(a) for a in records(s, wid, "artifact")],
                [view(a) for a in records(s, wid, "audit")][-30:],
            )

    @app.post("/api/w/{wid}/requirements", status_code=201)
    def requirement(wid: str, body: Requirement, user=Depends(identity)):
        access(wid, user, write=True)
        with db.transaction(wid) as s:
            return service.save_requirement(s, wid, body, user["actor"])

    @app.post("/api/w/{wid}/quotes", status_code=201)
    def quote(wid: str, body: QuoteInput, user=Depends(identity)):
        access(wid, user, write=True)
        with db.transaction(wid) as s:
            return service.save_quote(s, wid, body, user["actor"])

    @app.post("/api/w/{wid}/plans", status_code=201)
    def plan(wid: str, user=Depends(identity)):
        access(wid, user, write=True)
        with db.transaction(wid) as s:
            return service.plan(s, wid, user["actor"])

    @app.post("/api/w/{wid}/replan", status_code=201)
    def replan(wid: str, body: ReplanInput, user=Depends(identity)):
        access(wid, user, write=True)
        with db.transaction(wid) as s:
            return service.plan(s, wid, user["actor"], body.previous_plan_id)

    @app.get("/api/model-status")
    def model_status(user=Depends(identity)):
        required = ("MODEL_BASE_URL", "MODEL_NAME", "MODEL_API_KEY")
        missing = [key for key in required if not os.getenv(key)]
        return {
            "configured": not missing,
            "missing_variables": missing,
            "model_name": os.getenv("MODEL_NAME"),
            "worker_mode": worker_mode,
        }

    @app.get("/api/search-status")
    def search_status(user=Depends(identity)):
        from .search_index import MODEL, MODEL_REVISION, DIMENSION, embedding_cache
        from .settings import search_backend

        cache = (
            embedding_cache()
            / "models--Qdrant--bge-small-zh-v1.5/snapshots"
            / MODEL_REVISION
            / "model_optimized.onnx"
        )
        return {
            "backend": search_backend(),
            "embedding_model": MODEL,
            "dimension": DIMENSION,
            "model_cached": cache.is_file(),
        }

    @app.post("/api/w/{wid}/plans/{pid}/approve")
    def approve(wid: str, pid: str, user=Depends(identity)):
        access(wid, user, approval=True)
        with db.transaction(wid) as s:
            return service.approve(s, wid, pid, user["actor"])

    @app.post("/api/w/{wid}/plans/{pid}/execute")
    def execute(wid: str, pid: str, body: Execution, user=Depends(identity)):
        access(wid, user, write=True)
        with db.transaction(wid) as s:
            return service.execute(s, wid, pid, body.idempotency_key, user["actor"])

    @app.post("/api/w/{wid}/orders/{oid}/receipts", status_code=201)
    def receive(wid: str, oid: str, body: ReceiptInput, user=Depends(identity)):
        access(wid, user, write=True)
        with db.transaction(wid) as s:
            return service.receive(s, wid, oid, body, user["actor"])

    @app.post("/api/w/{wid}/orders/{oid}/cancel")
    def cancel(wid: str, oid: str, user=Depends(identity)):
        access(wid, user, approval=True)
        with db.transaction(wid) as s:
            return service.cancel(s, wid, oid, user["actor"])

    @app.post("/api/w/{wid}/exceptions/{eid}/resolve")
    def resolve(wid: str, eid: str, body: ResolutionInput, user=Depends(identity)):
        access(wid, user, approval=True)
        with db.transaction(wid) as s:
            return service.resolve(s, wid, eid, body, user["actor"])

    @app.get("/api/w/{wid}/orders/{oid}/export")
    def export(wid: str, oid: str, user=Depends(identity)):
        access(wid, user)
        with db.transaction(wid) as s:
            order = service.get(s, wid, "order", oid)
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["product", "supplier", "quantity", "unit_price_cents"])
            for line in order.data["lines"]:
                # CSV 单元格公式转义，供应商名称属于不可信输入。
                safe = lambda v: (
                    "'" + v
                    if isinstance(v, str)
                    and v.startswith(("=", "+", "-", "@", "\t", "\r"))
                    else v
                )
                writer.writerow(
                    [
                        safe(line["name"]),
                        safe(line["supplier"]),
                        line["quantity"],
                        line["price_cents"],
                    ]
                )
            return Response(
                output.getvalue().encode("utf-8-sig"),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="order-{oid}.csv"'
                },
            )

    @app.post("/api/w/{wid}/dispatch")
    def dispatch(wid: str, user=Depends(identity)):
        access(wid, user, write=True)
        with db.transaction(wid) as s:
            return service.dispatch(s, wid)

    @app.post("/api/w/{wid}/runs", status_code=201)
    def start(wid: str, body: RunInput, user=Depends(identity)):
        access(wid, user, write=True)
        with db.transaction(wid) as s:
            return runtime.start(s, wid, user["actor"], body.max_steps)

    def submit_analysis(wid, body, user, queued=False):
        access(wid, user, write=True)
        if not all(
            os.getenv(k) for k in ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME")
        ):
            raise HTTPException(
                503, "请在本机设置 MODEL_BASE_URL、MODEL_API_KEY、MODEL_NAME"
            )
        pricing = None
        if os.getenv("MODEL_INPUT_PRICE_PER_MILLION") and os.getenv(
            "MODEL_OUTPUT_PRICE_PER_MILLION"
        ):
            pricing = {
                "input": os.environ["MODEL_INPUT_PRICE_PER_MILLION"],
                "output": os.environ["MODEL_OUTPUT_PRICE_PER_MILLION"],
            }
        return analysis.run(
            db,
            wid,
            user["actor"],
            pricing=pricing,
            max_cost=os.getenv("MODEL_MAX_COST"),
            max_seconds=int(os.getenv("MODEL_RUN_TIMEOUT_SECONDS", "300")),
            max_tokens=(body or AnalysisOptions()).max_tokens,
            max_steps=(body or AnalysisOptions()).max_steps,
            prepare_only=queued,
            coordination_mode=(body or AnalysisOptions()).coordination_mode,
            catalog_mode=(body or AnalysisOptions()).catalog_mode,
            context_excerpt=(body or AnalysisOptions()).context_excerpt,
            idempotency_key=body.idempotency_key if queued else None,
        )

    @app.post("/api/w/{wid}/analyses")
    def analyze(wid: str, body: AnalysisOptions | None = None, user=Depends(identity)):
        return submit_analysis(wid, body, user)

    @app.post("/api/w/{wid}/analysis-jobs", status_code=202)
    def enqueue_analysis(wid: str, body: AnalysisJobInput, user=Depends(identity)):
        if worker_mode == "disabled":
            raise HTTPException(503, "后台执行已禁用")
        return submit_analysis(wid, body, user, queued=True)

    @app.get("/api/w/{wid}/analyses/{aid}")
    def analysis_status(wid: str, aid: str, user=Depends(identity)):
        access(wid, user)
        from .jobs import public

        with db.transaction(wid) as s:
            return public(service.get(s, wid, "analysis", aid))

    @app.get("/api/w/{wid}/analysis-events")
    def analysis_events(
        wid: str,
        last_event_id: Annotated[str | None, Header()] = None,
        user=Depends(identity),
    ):
        access(wid, user)
        from .progress import events

        return StreamingResponse(
            events(db.engine, wid, last_event_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/w/{wid}/analysis-jobs/{aid}/resume", status_code=202)
    def enqueue_resume(wid: str, aid: str, user=Depends(identity)):
        access(wid, user, write=True)
        if worker_mode == "disabled":
            raise HTTPException(503, "后台执行已禁用")
        from .jobs import requeue

        with db.transaction(wid) as s:
            return requeue(s, wid, service.get(s, wid, "analysis", aid), user["actor"])

    @app.post("/api/w/{wid}/analysis-jobs/{aid}/continue", status_code=202)
    def enqueue_continue(
        wid: str, aid: str, body: ContinuationInput, user=Depends(identity)
    ):
        access(wid, user, write=True)
        if worker_mode == "disabled":
            raise HTTPException(503, "后台执行已禁用")
        from .continuation import continue_truncated

        return continue_truncated(
            db, wid, aid, user["actor"], body.seconds, prepare_only=True
        )

    @app.post("/api/model-check")
    def model_check(user=Depends(identity)):
        if user["role"] not in ("buyer", "admin"):
            raise HTTPException(403, "模型自检需要采购员或管理员权限")
        if not all(
            os.getenv(k) for k in ("MODEL_BASE_URL", "MODEL_NAME", "MODEL_API_KEY")
        ):
            raise HTTPException(503, "模型服务尚未配置")
        from .model_check import check

        return check()

    @app.post("/api/w/{wid}/analyses/{aid}/resume")
    def analysis_resume(wid: str, aid: str, user=Depends(identity)):
        access(wid, user, write=True)
        if not all(
            os.getenv(k) for k in ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME")
        ):
            raise HTTPException(503, "请先配置模型环境变量")
        return analysis.run(db, wid, user["actor"], task_id=aid)

    @app.post("/api/w/{wid}/analyses/{aid}/continue")
    def analysis_continue(
        wid: str, aid: str, body: ContinuationInput, user=Depends(identity)
    ):
        access(wid, user, write=True)
        if not all(
            os.getenv(k) for k in ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME")
        ):
            raise HTTPException(503, "请先配置模型环境变量")
        from .continuation import continue_truncated

        return continue_truncated(db, wid, aid, user["actor"], body.seconds)

    @app.post("/api/w/{wid}/analyses/{aid}/cancel")
    def analysis_cancel(wid: str, aid: str, user=Depends(identity)):
        access(wid, user, write=True)
        with db.transaction(wid) as s:
            task = service.get(s, wid, "analysis", aid)
            if task.data["state"] in (
                "queued",
                "running",
                "waiting_provider",
                "waiting_response",
            ):
                task.data = {**task.data, "state": "cancelled"}
                if task.data.get("dispatch_state") == "queued":
                    task.data = {**task.data, "dispatch_state": "done"}
                service.audit(s, wid, user["actor"], "analysis.cancelled", aid)
            from .jobs import public

            return public(task)

    @app.post("/api/w/{wid}/runs/{rid}/resume")
    def resume(wid: str, rid: str, user=Depends(identity)):
        access(wid, user, write=True)
        return runtime.resume(db, wid, rid)

    @app.get("/api/w/{wid}/runs/{rid}/events")
    def events(wid: str, rid: str, user=Depends(identity)):
        """有界 SSE 轨迹重放。客户端断线后重新读取，事件 ID 稳定。"""
        access(wid, user)
        with db.transaction(wid) as s:
            service.get(s, wid, "run", rid)
            payload = "".join(
                f"id: {r.id}\nevent: trace\ndata: {json.dumps(view(r), ensure_ascii=False)}\n\n"
                for r in records(s, wid, "trace")
                if r.data["run_id"] == rid
            )
        return StreamingResponse(
            iter([payload]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    static = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if static.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=static / "assets", check_dir=False),
            name="assets",
        )

        @app.get("/")
        def index():
            return FileResponse(static / "index.html")

    return app
