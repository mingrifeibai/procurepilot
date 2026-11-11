"""采购工作区和文档记录基线；兼容 0.1 的既有表，不删除既有数据。"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    expected = {
        "workspaces": {"id", "name"},
        "records": {"id", "workspace_id", "kind", "sequence", "data", "created_at"},
    }
    for name, columns in expected.items():
        if inspector.has_table(name):
            actual = {c["name"] for c in inspector.get_columns(name)}
            if actual != columns:
                raise RuntimeError(f"{name} 表结构与 0.1 基线不符，停止自动迁移")
    if not inspector.has_table("workspaces"):
        op.create_table(
            "workspaces",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(100), nullable=False),
        )
    if not inspector.has_table("records"):
        op.create_table(
            "records",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("workspace_id", sa.String(36), nullable=False),
            sa.Column("kind", sa.String(30), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
        )
    existing = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("records")}
    for column in ("workspace_id", "kind"):
        name = "ix_records_" + column
        if name not in existing:
            op.create_index(name, "records", [column])


def downgrade():
    raise RuntimeError("基线包含业务数据，不提供自动删除式降级；请使用备份恢复")
