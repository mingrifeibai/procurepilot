"""组织归属、数据范围与组织成员凭据。"""

from secrets import token_urlsafe
from typing import Literal
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select

from .domain import StrictModel
from .members import digest
from .service import fail
from .storage import Organization, OrganizationMember, Workspace, now


class OrganizationInput(StrictModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")


class OrganizationMemberInput(StrictModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"\S")
    role: Literal["admin", "buyer", "approver", "viewer"]


def platform_admin(user):
    return (
        user["role"] == "admin"
        and not user.get("organization_id")
        and "*" in user.get("workspaces", [])
    )


def workspace_scope(user):
    """组织限制和工作区授权取交集。旧全局演示令牌只覆盖历史工作区。"""
    query = select(Workspace)
    grants = user.get("workspaces", [])
    if "*" not in grants:
        query = query.where(Workspace.id.in_(grants))
    if user.get("organization_id"):
        query = query.where(Workspace.organization_id == user["organization_id"])
    elif "*" in grants and not platform_admin(user):
        query = query.where(Workspace.organization_id.is_(None))
    return query


def public(organization):
    return {"id": organization.id, "name": organization.name}


def member_public(member):
    return {
        "id": member.id,
        "name": member.name,
        "role": member.role,
        "created_at": member.created_at,
        "issued_by": member.issued_by,
        "revoked_at": member.revoked_at,
        "revoked_by": member.revoked_by,
    }


def authorize_admin(session, oid, user, lock=False):
    if not platform_admin(user) and (
        user["role"] != "admin"
        or user.get("organization_id") != oid
        or "*" not in user.get("workspaces", [])
    ):
        fail("无此组织的管理权限", 403)
    query = select(Organization).where(Organization.id == oid)
    if lock:
        query = query.with_for_update()
    if session.scalar(query) is None:
        fail("组织不存在", 404)


def issue(session, oid, user, body):
    authorize_admin(session, oid, user, lock=True)
    if body.role == "admin" and not platform_admin(user):
        fail("组织管理员由平台管理员授权", 403)
    token = "pporg_" + token_urlsafe(32)
    member = OrganizationMember(
        id=str(uuid4()),
        organization_id=oid,
        name=body.name.strip(),
        role=body.role,
        token_sha256=digest(token),
        created_at=now(),
        issued_by=user["actor"],
    )
    session.add(member)
    return {**member_public(member), "token": token}


def revoke(session, oid, mid, user):
    authorize_admin(session, oid, user, lock=True)
    member = session.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == oid,
            OrganizationMember.id == mid,
        )
    )
    if member is None:
        fail("组织成员不存在", 404)
    if member.role == "admin" and not platform_admin(user):
        fail("组织管理员由平台管理员撤销", 403)
    if member.revoked_at is None:
        member.revoked_at = now()
        member.revoked_by = user["actor"]
    return member_public(member)


def authenticate(session, token):
    if len(token) != 49:
        return None
    member = session.scalar(
        select(OrganizationMember).where(
            OrganizationMember.token_sha256 == digest(token),
            OrganizationMember.revoked_at.is_(None),
        )
    )
    if member is None:
        return None
    return {
        "name": member.name,
        "actor": "organization-member:" + member.id,
        "role": member.role,
        "workspaces": ["*"],
        "organization_id": member.organization_id,
    }
