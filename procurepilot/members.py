"""工作区成员凭据。仅保存随机令牌的摘要，发放与撤销写入审计。"""

from hashlib import sha256
from secrets import token_urlsafe
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from .domain import StrictModel
from .service import audit, fail, get
from .storage import Record, add, now, records


class MemberInput(StrictModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"\S")
    role: Literal["buyer", "approver", "viewer"]


def digest(token):
    return sha256(token.encode()).hexdigest()


def public(member):
    return {
        "id": member.id,
        "name": member.data["name"],
        "role": member.data["role"],
        "created_at": member.created_at,
        "revoked_at": member.data.get("revoked_at"),
    }


def issue(session, wid, actor, body):
    if (
        sum(not m.data.get("revoked_at") for m in records(session, wid, "member"))
        >= 100
    ):
        fail("工作区已达到100个有效成员，请先撤销停用成员", 409)
    token = "ppw_" + token_urlsafe(32)
    member = add(
        session,
        wid,
        "member",
        {
            "name": body.name.strip(),
            "role": body.role,
            "token_sha256": digest(token),
        },
    )
    audit(session, wid, actor, "member.issued", member.id)
    return {**public(member), "token": token}


def revoke(session, wid, actor, mid):
    member = get(session, wid, "member", mid)
    if not member.data.get("revoked_at"):
        member.data = {**member.data, "revoked_at": now()}
        audit(session, wid, actor, "member.revoked", mid)
    return public(member)


def authenticate(session, token):
    if not token.startswith("ppw_") or len(token) != 47:
        return None
    member = session.scalar(
        select(Record).where(
            Record.kind == "member",
            Record.data["token_sha256"].as_string() == digest(token),
        )
    )
    if member is None or member.data.get("revoked_at"):
        return None
    return {
        "name": member.data["name"],
        "actor": "member:" + member.id,
        "role": member.data["role"],
        "workspaces": [member.workspace_id],
    }
