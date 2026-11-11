"""验证空库升级、旧库接管和重复启动均保留原始业务数据。"""

from sqlalchemy import create_engine, text, inspect
from procurepilot.storage import Store


def test_existing_01_database_is_adopted_without_loss(tmp_path):
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                "create table workspaces (id varchar(36) primary key, name varchar(100) not null)"
            )
        )
        c.execute(
            text(
                "create table records (id varchar(36) primary key, workspace_id varchar(36) not null, "
                "kind varchar(30) not null, sequence integer not null, data json not null, created_at varchar(40) not null)"
            )
        )
        c.execute(
            text("insert into workspaces(id,name) values ('original','历史工作区')")
        )
        c.execute(
            text(
                """insert into records values ('record','original','request',1,'{"title":"保留记录"}','2026-01-01')"""
            )
        )
    engine.dispose()
    for _ in range(2):
        store = Store(url)
        with store.engine.connect() as c:
            assert c.scalar(text("select name from workspaces")) == "历史工作区"
            assert c.scalar(text("select organization_id from workspaces")) is None
            assert c.scalar(text("select version_num from alembic_version")) == "0003"
            assert c.scalar(text("select workspace_id from records")) == "original"
        assert any(
            fk["referred_table"] == "organizations"
            for fk in inspect(store.engine).get_foreign_keys("workspaces")
        )
        assert "ix_records_scope_time" in {
            i["name"] for i in inspect(store.engine).get_indexes("records")
        }
        store.engine.dispose()


def test_incompatible_database_is_rejected(tmp_path):
    import pytest

    url = f"sqlite:///{(tmp_path / 'bad.db').as_posix()}"
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("create table workspaces (wrong_column text)"))
    engine.dispose()
    with pytest.raises(RuntimeError, match="表结构"):
        Store(url)
