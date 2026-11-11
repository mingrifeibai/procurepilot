"""覆盖发放、登录、跨工作区拒绝、撤销和密钥泄漏边界。"""

import json
from fastapi.testclient import TestClient

from procurepilot.api import create_app
from procurepilot.storage import records


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_workspace_member_lifecycle(env, monkeypatch):
    client, db, wid = env
    other = client.post("/api/workspaces", json={"name": "另一工作区"}).json()["id"]
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv(
        "API_TOKENS_JSON",
        json.dumps(
            {
                "fixture-admin": {"role": "admin", "workspaces": [wid]},
            }
        ),
    )
    client = TestClient(create_app(db))
    prefix = f"/api/w/{wid}"
    request = {"name": "采购助理", "role": "viewer"}
    assert (
        client.post(
            f"/api/w/{other}/members", json=request, headers=auth("fixture-admin")
        ).status_code
        == 403
    )
    response = client.post(
        prefix + "/members", json=request, headers=auth("fixture-admin")
    )
    assert (
        response.status_code == 201 and response.headers["Cache-Control"] == "no-store"
    )
    issued = response.json()
    token = issued["token"]
    member = auth(token)
    assert client.get("/api/session", headers=member).json()["role"] == "viewer"
    assert [w["id"] for w in client.get("/api/workspaces", headers=member).json()] == [
        wid
    ]
    assert client.get(f"/api/w/{other}/snapshot", headers=member).status_code == 403
    assert client.post(prefix + "/plans", headers=member).status_code == 403
    assert client.get(prefix + "/members", headers=member).status_code == 403
    public = client.get(prefix + "/members", headers=auth("fixture-admin")).json()[0]
    assert "token" not in public and "token_sha256" not in public
    with db.transaction(wid) as session:
        saved = records(session, wid, "member")[0].data
        assert token not in json.dumps(saved) and len(saved["token_sha256"]) == 64
    revoke = client.delete(
        prefix + f"/members/{issued['id']}", headers=auth("fixture-admin")
    )
    assert revoke.status_code == 200 and revoke.json()["revoked_at"]
    assert client.get("/api/session", headers=member).status_code == 401
    assert client.get("/api/session", headers=auth("demo-buyer")).status_code == 401
