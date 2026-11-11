"""事务存储。开发使用 SQLite WAL，部署可切换 PostgreSQL。

工作区行锁串行化库存、审批和订单变更；SQLite 使用 BEGIN IMMEDIATE
提前取得写锁，避免先读后写造成重复消费库存。
"""

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import create_engine, String, JSON, Integer, ForeignKey, select, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(12))
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[str] = mapped_column(String(40))
    issued_by: Mapped[str] = mapped_column(String(256))
    revoked_at: Mapped[str | None] = mapped_column(String(40))
    revoked_by: Mapped[str | None] = mapped_column(String(256))


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )


class Record(Base):
    """追加版本和运行快照的统一文档表，workspace_id 是强制隔离边界。"""

    __tablename__ = "records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, url=None):
        url = url or os.getenv("DATABASE_URL", "sqlite:///procurepilot.db")
        dialect = make_url(url).get_backend_name()
        if os.getenv("REQUIRE_POSTGRES") == "1" and dialect != "postgresql":
            raise RuntimeError("无状态部署需要 PostgreSQL 数据库")
        self.engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30}
            if dialect == "sqlite"
            else {},
        )
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def configure(connection, _):
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")

        from .migrations import migrate

        migrate(self.engine)

    @contextmanager
    def transaction(self, workspace_id=None):
        """异常自动回滚，订单、库存和 Outbox 在同一提交边界内。"""
        with Session(self.engine, expire_on_commit=False) as session:
            try:
                if self.engine.dialect.name == "sqlite":
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                elif workspace_id:
                    session.execute(
                        select(Workspace)
                        .where(Workspace.id == workspace_id)
                        .with_for_update()
                    )
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise


def add(session, workspace_id, kind, data):
    record = Record(
        id=str(uuid4()),
        workspace_id=workspace_id,
        kind=kind,
        data=data,
        created_at=now(),
    )
    session.add(record)
    session.flush()
    return record


def records(session, workspace_id, kind):
    return list(
        session.scalars(
            select(Record)
            .where(Record.workspace_id == workspace_id, Record.kind == kind)
            .order_by(Record.created_at, Record.id)
        )
    )


def view(record):
    return {"id": record.id, "created_at": record.created_at, **record.data}
