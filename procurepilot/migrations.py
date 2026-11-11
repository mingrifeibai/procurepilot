"""以同一数据库连接运行 Alembic，保留测试 schema 和事务配置。"""

from pathlib import Path
from alembic import command
from alembic.config import Config


def migrate(engine):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "alembic"))
    with engine.begin() as connection:
        # 多副本同时启动时串行化 DDL；业务写锁不能保护尚未创建的表。
        if engine.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        elif engine.dialect.name == "postgresql":
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(72489632)")
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
