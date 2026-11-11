"""迁移环境只使用调用方连接，避免泄漏数据库口令或连接到错误库。"""

from alembic import context
from procurepilot.storage import Base

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("请使用 python -m procurepilot.manage migrate 运行迁移")
context.configure(
    connection=connection, target_metadata=Base.metadata, compare_type=True
)
with context.begin_transaction():
    context.run_migrations()
