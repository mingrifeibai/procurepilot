"""通过真实 HTTP 路由覆盖组织边界、角色和凭据撤销。"""

import json

from fastapi.testclient import TestClient

from procurepilot.api import create_app
from procurepilot.members import digest
from procurepilot.storage import OrganizationMember


def test_organization_roles_and_workspace_isolation(env, monkeypatch):
    _, store, legacy = env
    tokens = {
        "fixture-platform": {"name": "平台管理员", "role": "admin", "workspaces": ["*"]}
    }
    monkeypatch.setenv("API_TOKENS_JSON", json.dumps(tokens))
    client = TestClient(create_app(store))

    def request(method, path, token, expected, body=None):
        response = client.request(
            method,
            f"/api{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        assert response.status_code == expected, response.text
        return response

    platform = "fixture-platform"
    first, second = (
        request("POST", "/organizations", platform, 201, {"name": name}).json()["id"]
        for name in ("甲公司", "乙公司")
    )
    members = f"/organizations/{first}/members"

    def issue(path, token, role):
        response = request("POST", path, token, 201, {"name": role, "role": role})
        assert response.headers["Cache-Control"] == "no-store"
        return response.json()

    admin = issue(members, platform, "admin")
    assert admin["issued_by"] == "平台管理员"
    buyer = issue(members, admin["token"], "buyer")
    approver = issue(members, admin["token"], "approver")
    viewer = issue(members, admin["token"], "viewer")
    other = issue(f"/organizations/{second}/members", platform, "buyer")
    assert buyer["issued_by"] == f"organization-member:{admin['id']}"
    request("POST", members, admin["token"], 403, {"name": "管理员", "role": "admin"})
    request("GET", members, buyer["token"], 403)
    request("GET", f"/organizations/{second}/members", admin["token"], 403)
    request("POST", "/organizations", admin["token"], 403, {"name": "越权组织"})
    request(
        "POST",
        "/workspaces",
        buyer["token"],
        403,
        {"name": "跨组织", "organization_id": second},
    )
    request("POST", "/workspaces", viewer["token"], 403, {"name": "越权写入"})
    request(
        "POST",
        "/workspaces",
        "demo-buyer",
        403,
        {"name": "越权", "organization_id": first},
    )

    left = request(
        "POST", "/workspaces", buyer["token"], 201, {"name": "采购一组"}
    ).json()
    right = request(
        "POST", "/workspaces", other["token"], 201, {"name": "采购二组"}
    ).json()
    assert left["organization_id"] == first
    assert right["organization_id"] == second
    assert request("GET", "/workspaces", viewer["token"], 200).json() == [left]
    assert request("GET", "/organizations", viewer["token"], 200).json() == [
        {"id": first, "name": "甲公司"}
    ]
    assert {
        row["id"] for row in request("GET", "/workspaces", platform, 200).json()
    } == {legacy, left["id"], right["id"]}
    assert [
        row["id"] for row in request("GET", "/workspaces", "demo-buyer", 200).json()
    ] == [legacy]

    left_path, right_path = f"/w/{left['id']}", f"/w/{right['id']}"
    request("GET", f"{left_path}/snapshot", viewer["token"], 200)
    request("GET", f"{left_path}/snapshot", other["token"], 404)
    request("GET", f"{right_path}/snapshot", admin["token"], 404)
    request("GET", f"/w/{legacy}/snapshot", buyer["token"], 404)
    request("POST", f"{left_path}/runs", viewer["token"], 403, {"max_steps": 8})
    request("POST", f"{left_path}/plans/missing/approve", buyer["token"], 403)
    request("POST", f"{left_path}/plans/missing/approve", approver["token"], 404)
    scoped = issue(f"{left_path}/members", admin["token"], "viewer")
    assert request("GET", "/workspaces", scoped["token"], 200).json() == [left]
    request("GET", f"{right_path}/snapshot", scoped["token"], 403)
    request("GET", members, scoped["token"], 403)

    request("DELETE", f"{members}/{admin['id']}", admin["token"], 403)
    request("DELETE", f"{members}/{other['id']}", platform, 404)
    removed = request("DELETE", f"{members}/{viewer['id']}", admin["token"], 200).json()
    assert (
        removed["revoked_at"]
        and removed["revoked_by"] == f"organization-member:{admin['id']}"
    )
    assert (
        request("DELETE", f"{members}/{viewer['id']}", platform, 200).json() == removed
    )
    request("GET", "/workspaces", viewer["token"], 401)
    roster = request("GET", members, admin["token"], 200).json()
    assert all("token" not in row and "token_sha256" not in row for row in roster)
    with store.transaction() as session:
        assert session.get(OrganizationMember, buyer["id"]).token_sha256 == digest(
            buyer["token"]
        )

    tokens["limited-admin"] = {
        "role": "admin",
        "workspaces": [left["id"]],
        "organization_id": first,
    }
    monkeypatch.setenv("API_TOKENS_JSON", json.dumps(tokens))
    client = TestClient(create_app(store))
    request("GET", f"{left_path}/snapshot", "limited-admin", 200)
    request("GET", members, "limited-admin", 403)
