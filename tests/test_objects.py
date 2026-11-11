"""原件完整性、去重与工作区下载权限。"""

import base64
from contextlib import closing
from hashlib import sha256
import os
from uuid import uuid4
import pytest
from fastapi import HTTPException
from procurepilot.objects import LocalObjects


def test_content_addressing_roundtrip_and_corruption():
    store = LocalObjects()
    raw = b"\xef\xbb\xbfquotation source"
    metadata = store.put(raw)
    assert metadata["key"] == sha256(raw).hexdigest()
    assert store.put(raw) == metadata and store.get(metadata) == raw
    store.path(metadata["key"]).write_bytes(b"changed")
    with pytest.raises(HTTPException) as error:
        store.get(metadata)
    assert error.value.status_code == 503


def test_object_keys_cannot_escape_storage():
    with pytest.raises(HTTPException):
        LocalObjects().path("../outside")


def test_original_download_is_scoped_and_preserves_bytes(env):
    c, _, w = env
    raw = "\ufeff供应商原始报价\n".encode("utf-8")
    item = c.post(
        f"/api/w/{w}/attachments",
        json={
            "filename": "中文报价.txt",
            "content_base64": base64.b64encode(raw).decode(),
        },
    ).json()
    result = c.get(f"/api/w/{w}/attachments/{item['id']}/original")
    assert result.status_code == 200 and result.content == raw
    assert result.headers["x-content-type-options"] == "nosniff"
    other = c.post("/api/workspaces", json={"name": "other"}).json()["id"]
    assert c.get(f"/api/w/{other}/attachments/{item['id']}/original").status_code == 404


def test_old_attachment_requires_reupload(env):
    from procurepilot.storage import add

    c, db, w = env
    with db.transaction(w) as session:
        item = add(
            session,
            w,
            "artifact",
            {"filename": "old.txt", "text": "old", "characters": 3},
        )
        aid = item.id
    assert c.get(f"/api/w/{w}/attachments/{aid}/original").status_code == 409


def test_real_s3_preserves_legacy_downloads_and_rejects_corruption(env, monkeypatch):
    """在独立桶前缀下通过实际附件 API 验证签名存储、去重和损坏检测。"""
    endpoint = os.getenv("TEST_OBJECT_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("真实 S3 兼容验收需要 TEST_OBJECT_S3_ENDPOINT")
    from fastapi.testclient import TestClient
    from procurepilot.api import create_app
    from procurepilot.object_migration import migrate
    from procurepilot.objects import S3Objects
    from procurepilot.storage import Store

    client, database, wid = env
    prefix = f"/api/w/{wid}/attachments"
    payload = {
        "filename": "原始报价.txt",
        "content_base64": base64.b64encode("合成原始报价\n".encode()).decode(),
    }
    monkeypatch.setenv("OBJECT_BACKEND", "local")
    old_bytes = "旧本地合成文件\n".encode()
    old = (
        client.post(
            prefix,
            json={**payload, "content_base64": base64.b64encode(old_bytes).decode()},
        )
        .raise_for_status()
        .json()
    )
    old_ref = client.get(prefix).raise_for_status().json()[0]["object"]
    monkeypatch.setenv("OBJECT_BACKEND", "s3")
    monkeypatch.setenv("OBJECT_S3_ENDPOINT", endpoint)
    monkeypatch.setenv("OBJECT_S3_BUCKET", os.environ["TEST_OBJECT_S3_BUCKET"])
    monkeypatch.setenv("OBJECT_S3_PREFIX", "test-" + uuid4().hex)
    raw = base64.b64decode(payload["content_base64"])
    store = S3Objects()
    try:
        first = client.post(prefix, json=payload).raise_for_status().json()
        second = client.post(prefix, json=payload).raise_for_status().json()
        items = client.get(prefix).raise_for_status().json()
        remote = [item["object"] for item in items if item["object"]["backend"] == "s3"]
        assert len(remote) == 2 and remote[0] == remote[1]
        assert (
            client.get(f"{prefix}/{old['id']}/original").raise_for_status().content
            == old_bytes
        )
        for item in (first, second):
            assert (
                client.get(f"{prefix}/{item['id']}/original").raise_for_status().content
                == raw
            )

        other = (
            client.post("/api/workspaces", json={"name": "隔离工作区"})
            .raise_for_status()
            .json()
        )
        assert (
            client.get(
                f"/api/w/{other['id']}/attachments/{first['id']}/original"
            ).status_code
            == 404
        )
        with closing(store._client()) as s3:
            result = s3.list_objects_v2(Bucket=store.bucket, Prefix=store.prefix + "/")
            assert result["KeyCount"] == 1
            s3.put_object(
                Bucket=store.bucket,
                Key=result["Contents"][0]["Key"],
                Body=b"x" * len(raw),
            )
        assert client.get(f"{prefix}/{first['id']}/original").status_code == 503
        assert client.post(prefix, json=payload).status_code == 503
        assert len(client.get(prefix).raise_for_status().json()) == 3
        assert migrate(database)["selected"] == 1
        assert migrate(database, apply=True)["moved"] == 1
        assert migrate(database, apply=True)["selected"] == 0
        assert LocalObjects().get(old_ref) == old_bytes
        replica = Store(database.engine.url)
        try:
            with monkeypatch.context() as replica_env:
                replica_env.setenv(
                    "OBJECT_ROOT", os.environ["OBJECT_ROOT"] + "-replica"
                )
                another = TestClient(
                    create_app(replica), headers={"Authorization": "Bearer demo-buyer"}
                )
                assert (
                    another.get(f"{prefix}/{old['id']}/original")
                    .raise_for_status()
                    .content
                    == old_bytes
                )
        finally:
            replica.engine.dispose()
    finally:
        with closing(store._client()) as s3:
            result = s3.list_objects_v2(Bucket=store.bucket, Prefix=store.prefix + "/")
            for item in result.get("Contents", []):
                s3.delete_object(Bucket=store.bucket, Key=item["Key"])


def test_stateless_database_configuration_rejects_sqlite(tmp_path, monkeypatch):
    from procurepilot.storage import Store

    monkeypatch.setenv("REQUIRE_POSTGRES", "1")
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        Store(f"sqlite:///{(tmp_path / 'unexpected.db').as_posix()}")
