"""SQLite / PostgreSQL 隔离测试夹具。"""

import os
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, text
from fastapi.testclient import TestClient
from procurepilot.api import create_app
from procurepilot.storage import Store


@pytest.fixture(autouse=True)
def isolate_raw_objects(tmp_path, monkeypatch):
    monkeypatch.setenv("OBJECT_ROOT", str(tmp_path / "objects"))


@pytest.fixture(scope="session", autouse=True)
def check_external_database():
    """指定 PostgreSQL 验收时先探活，避免每个用例都等待连接超时。"""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        return
    engine = create_engine(url, connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        pytest.exit(
            "指定的 PostgreSQL 测试库不可用；未执行数据库验收，请启动数据库后重试",
            returncode=2,
        )
    finally:
        engine.dispose()


@pytest.fixture
def env(tmp_path):
    url = os.getenv("TEST_DATABASE_URL")
    admin = None
    schema = "test_" + uuid4().hex
    if url:
        admin = create_engine(url, connect_args={"connect_timeout": 3})
        with admin.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA {schema}"))
        store = Store(
            url
            + (
                "?options=-csearch_path%3D"
                if "?" not in url
                else "&options=-csearch_path%3D"
            )
            + schema
            + "&connect_timeout=3"
        )
    else:
        store = Store(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    client = TestClient(create_app(store))
    client.headers["Authorization"] = "Bearer demo-buyer"
    w = client.post("/api/workspaces", json={"name": "测试工作区"}).json()["id"]
    yield client, store, w
    store.engine.dispose()
    if admin:
        with admin.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()
