"""偏好不能覆盖硬约束；澄清必须绑定输入版本，回答不能隐式改预算。"""

from datetime import datetime, timedelta, timezone
from .test_procurement import requirement
from .test_models import FakeModel
from procurepilot import analysis
from procurepilot.storage import records


def pref(c, w, value="A"):
    return c.post(
        f"/api/w/{w}/preferences",
        json={
            "key": "supplier",
            "value": value,
            "confirmed": True,
            "expires_in_days": 30,
        },
    )


def test_preference_version_and_deactivation(env):
    c, _, w = env
    one = pref(c, w).json()
    two = pref(c, w, "B").json()
    assert two["version"] == 2 and len(c.get(f"/api/w/{w}/preferences").json()) == 1
    assert c.delete(f"/api/w/{w}/preferences/{one['id']}").status_code == 409
    assert c.delete(f"/api/w/{w}/preferences/{two['id']}").json()["state"] == "inactive"


def test_expired_preference_not_sent_and_hard_budget_preserved(env):
    c, db, w = env
    requirement(c, w)
    pref(c, w).json()
    with db.transaction(w) as s:
        item = records(s, w, "preference")[-1]
        item.data = {
            **item.data,
            "expires_at": (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(),
        }
    assert c.get(f"/api/w/{w}/preferences").json()[0]["state"] == "expired"

    class Inspect(FakeModel):
        def assess(self, role, context, max_output):
            assert (
                context["preferences"] == []
                and context["requirement"]["budget_cny"] == "180000.00 元"
            )
            return super().assess(role, context, max_output)

    assert analysis.run(db, w, "test", Inspect())["state"] == "completed"


def test_new_preference_invalidates_inflight_snapshot(env):
    import pytest

    c, db, w = env
    requirement(c, w)
    with pytest.raises(InterruptedError):
        analysis.run(db, w, "test", FakeModel(), stop_after="采购协调员")
    with db.transaction(w) as s:
        aid = records(s, w, "analysis")[-1].id
    pref(c, w)
    assert analysis.run(db, w, "test", FakeModel(), task_id=aid)["state"] == "stale"


def test_clarification_answer_is_evidence_and_not_implicit_requirement_change(env):
    c, db, w = env
    requirement(c, w)
    assert (
        analysis.run(db, w, "test", FakeModel(True))["state"] == "evidence_insufficient"
    )
    questions = c.get(f"/api/w/{w}/clarifications").json()
    assert len(questions) == 1 and questions[0]["state"] == "open"
    response = c.post(
        f"/api/w/{w}/clarifications/{questions[0]['id']}/answer",
        json={"answer": "只按已列出的内存和接口约束比较，不增加显卡要求"},
    )
    assert response.status_code == 200
    assert len(c.get(f"/api/w/{w}/snapshot").json()["requirement"]) == 1

    class Inspect(FakeModel):
        def assess(self, role, context, max_output):
            assert len(context["confirmed_answers"]) == 1
            assert context["confirmed_answers"][0]["id"] in context["evidence_ids"]
            return super().assess(role, context, max_output)

    assert analysis.run(db, w, "test", Inspect())["state"] == "completed"


def test_old_requirement_question_cannot_change_new_requirement(env):
    c, db, w = env
    requirement(c, w)
    analysis.run(db, w, "test", FakeModel(True))
    q = c.get(f"/api/w/{w}/clarifications").json()[0]
    requirement(c, w, budget="100000")
    assert c.get(f"/api/w/{w}/clarifications").json()[0]["stale"] is True
    assert (
        c.post(
            f"/api/w/{w}/clarifications/{q['id']}/answer", json={"answer": "旧问题回答"}
        ).status_code
        == 409
    )


def test_cross_workspace_preference_isolation_and_roles(env):
    c, _, w = env
    p = pref(c, w).json()
    other = c.post("/api/workspaces", json={"name": "其他组织"}).json()["id"]
    assert c.get(f"/api/w/{other}/preferences").json() == []
    assert c.delete(f"/api/w/{other}/preferences/{p['id']}").status_code == 404
    assert pref(c, w).status_code == 201
    assert (
        c.post(
            f"/api/w/{w}/preferences",
            headers={"Authorization": "Bearer demo-viewer"},
            json={"key": "x", "value": "x", "confirmed": True},
        ).status_code
        == 403
    )


def test_structured_answer_creates_requirement_version(env):
    c, db, w = env
    requirement(c, w)
    analysis.run(db, w, "test", FakeModel(True))
    q = c.get(f"/api/w/{w}/clarifications").json()[0]
    body = {
        "answer": "已明确新的预算和数量",
        "requirement": {
            "title": "确认后采购",
            "budget": "100000",
            "delivery_days": 14,
            "needs": [
                {
                    "category": "computer",
                    "quantity": 1,
                    "min_memory_gb": 32,
                    "interface": "usb-c",
                }
            ],
        },
    }
    r = c.post(f"/api/w/{w}/clarifications/{q['id']}/answer", json=body)
    assert r.status_code == 200
    req = c.get(f"/api/w/{w}/snapshot").json()["requirement"]
    assert len(req) == 2 and req[-1]["budget_cents"] == 10000000
    assert c.get(f"/api/w/{w}/clarifications").json()[0]["stale"] is False
