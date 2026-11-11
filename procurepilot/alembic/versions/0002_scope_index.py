"""按工作区和类型读取文档时使用联合索引，避免扫描其他工作区。"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    names = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("records")}
    if "ix_records_scope_time" not in names:
        op.create_index(
            "ix_records_scope_time",
            "records",
            ["workspace_id", "kind", "created_at", "id"],
        )


def downgrade():
    op.drop_index("ix_records_scope_time", table_name="records")
