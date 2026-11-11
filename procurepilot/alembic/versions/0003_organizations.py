"""增加组织、组织成员和工作区归属，历史工作区保留原有访问范围。"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
    )
    op.create_table(
        "organization_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("role", sa.String(12), nullable=False),
        sa.Column("token_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("issued_by", sa.String(256), nullable=False),
        sa.Column("revoked_at", sa.String(40), nullable=True),
        sa.Column("revoked_by", sa.String(256), nullable=True),
    )
    op.create_index(
        "ix_organization_members_organization_id",
        "organization_members",
        ["organization_id"],
    )
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("organization_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_workspaces_organization_id",
            "organizations",
            ["organization_id"],
            ["id"],
        )
        batch.create_index("ix_workspaces_organization_id", ["organization_id"])


def downgrade():
    raise RuntimeError("组织权限和归属属于业务数据，请通过备份恢复")
