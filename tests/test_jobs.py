"""后台执行的真实事务竞争、租约故障和调用账本不变量；模型均为桩。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
import time
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from procurepilot import analysis, jobs
from procurepilot.api import create_app
from procurepilot.storage import records, add
from procurepilot.service import get
from .test_models import FakeModel
from .test_procurement import requirement


def queued(env, key="queued-test-01", **options):
    c, db, w = env
    if not c.get(f"/api/w/{w}/snapshot").json()["requirement"]:
        requirement(c, w)
    return analysis.run(
        db, w, "buyer", prepare_only=True, idempotency_key=key, **options
    )


def status(env, aid):
    return env[0].get(f"/api/w/{env[2]}/analyses/{aid}").json()


def expire(env, aid, **changes):
    _, db, w = env
    with db.transaction(w) as s:
        task = get(s, w, "analysis", aid)
        task.data = {
            **task.data,
            "worker_lease_until": (jobs.utcnow() - timedelta(seconds=1)).isoformat(),
            **changes,
        }


def test_enqueue_has_no_call_and_is_idempotent(env):
    r = queued(env)
    assert r["state"] == "queued" and r["tokens"] == 0
    assert queued(env)["id"] == r["id"]
    with pytest.raises(HTTPException):
        queued(env, max_tokens=24000)
    with env[1].transaction(env[2]) as s:
        assert len(records(s, env[2], "analysis")) == 1
        assert not records(s, env[2], "model_call")


def test_two_workers_one_task_and_safe_public_status(env):
    r = queued(env)
    model = FakeModel()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: jobs.run_once(env[1], model), range(2)))
    assert results.count(True) == 1 and model.calls == 3
    done = status(env, r["id"])
    assert done["state"] == "completed" and done["tokens"] == 450
    assert done["dispatch_state"] == "done"
    assert "context" not in done and "worker_token" not in done
    assert not env[0].get(f"/api/w/{env[2]}/snapshot").json()["order"]


def test_direct_resume_cannot_steal_queued_or_leased_task(env):
    r = queued(env)
    model = FakeModel()
    for claim in (False, True):
        if claim:
            jobs.claim(env[1])
        with pytest.raises(HTTPException):
            analysis.run(env[1], env[2], "buyer", model, task_id=r["id"])
    assert model.calls == 0


def test_expired_worker_is_fenced_and_safe_checkpoint_recovered(env):
    r = queued(env)
    _, aid, old_token, _ = jobs.claim(env[1])
    expire(env, aid)
    _, _, new_token, _ = jobs.claim(env[1])
    model = FakeModel()
    with pytest.raises(HTTPException):
        analysis.run(
            env[1], env[2], "buyer", model, task_id=aid, worker_token=old_token
        )
    done = analysis.run(
        env[1], env[2], "buyer", model, task_id=aid, worker_token=new_token
    )
    assert done["id"] == r["id"] and model.calls == 3


def test_worker_crash_with_pending_call_never_reissues(env):
    r = queued(env)
    jobs.claim(env[1])
    expire(env, r["id"])
    with env[1].transaction(env[2]) as s:
        add(
            s,
            env[2],
            "model_call",
            {
                "analysis_id": r["id"],
                "state": "pending",
                "lease_until": (jobs.utcnow() - timedelta(seconds=1)).isoformat(),
            },
        )
    model = FakeModel()
    assert jobs.run_once(env[1], model) is False
    assert model.calls == 0
    done = status(env, r["id"])
    assert done["uncertain_usage"] and done["state"] == "tool_unavailable"
    assert (
        env[0].post(f"/api/w/{env[2]}/analysis-jobs/{r['id']}/resume").status_code
        == 409
    )


def test_queue_deadline_and_requirement_change_make_no_calls(env):
    r = queued(env)
    expire(env, r["id"], deadline=(jobs.utcnow() - timedelta(seconds=1)).isoformat())
    model = FakeModel()
    assert jobs.run_once(env[1], model) is False
    assert status(env, r["id"])["state"] == "budget_exhausted"
    new = queued(env, "queued-test-02")
    requirement(env[0], env[2], budget="100000")
    assert jobs.run_once(env[1], model)
    assert status(env, new["id"])["state"] == "stale" and model.calls == 0


def test_cancel_queued_does_not_call_model(env):
    r = queued(env)
    assert (
        env[0].post(f"/api/w/{env[2]}/analyses/{r['id']}/cancel").json()["state"]
        == "cancelled"
    )
    model = FakeModel()
    assert not jobs.run_once(env[1], model) and model.calls == 0


def test_cancel_inflight_keeps_usage_and_stops_next_role(env):
    r = queued(env)
    started, release = Event(), Event()

    class Slow(FakeModel):
        def assess(self, *args):
            started.set()
            assert release.wait(5)
            return super().assess(*args)

    model = Slow()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(jobs.run_once, env[1], model)
        assert started.wait(5)
        try:
            assert status(env, r["id"])["state"] == "running"
            env[0].post(f"/api/w/{env[2]}/analyses/{r['id']}/cancel")
        finally:
            release.set()
        assert future.result(timeout=5)
    done = status(env, r["id"])
    assert done["state"] == "cancelled" and done["tokens"] == 150 and model.calls == 1


def test_checkpoint_resume_preserves_successful_role_usage(env):
    r = queued(env)
    _, aid, token, _ = jobs.claim(env[1])
    model = FakeModel()
    with pytest.raises(InterruptedError):
        analysis.run(
            env[1],
            env[2],
            "buyer",
            model,
            task_id=aid,
            worker_token=token,
            stop_after="采购协调员",
        )
    expire(env, aid)
    assert jobs.run_once(env[1], model)
    done = status(env, r["id"])
    assert model.calls == 3 and done["tokens"] == 450 and done["dispatch_attempts"] == 2


def test_async_api_returns_202_permissions_and_workspace_isolation(env, monkeypatch):
    c, _, w = env
    requirement(c, w)
    for key in ("MODEL_API_KEY", "MODEL_NAME", "MODEL_BASE_URL"):
        monkeypatch.setenv(key, "test-not-a-real-credential")
    body = {"idempotency_key": "http-test-001"}
    response = c.post(f"/api/w/{w}/analysis-jobs", json=body)
    assert response.status_code == 202
    assert (
        c.post(f"/api/w/{w}/analysis-jobs", json=body).json()["id"]
        == response.json()["id"]
    )
    other = c.post("/api/workspaces", json={"name": "other"}).json()["id"]
    assert c.get(f"/api/w/{other}/analyses/{response.json()['id']}").status_code == 404
    c.headers["Authorization"] = "Bearer demo-viewer"
    assert c.post(f"/api/w/{w}/analysis-jobs", json=body).status_code == 403


def test_embedded_worker_runs_in_lifespan(env, monkeypatch):
    r = queued(env)
    model = FakeModel()
    monkeypatch.setattr(analysis, "ChatCompletionsModel", lambda: model)
    with TestClient(create_app(env[1])) as client:
        client.headers["Authorization"] = "Bearer demo-buyer"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = client.get(f"/api/w/{env[2]}/analyses/{r['id']}").json()
            if result.get("dispatch_state") == "done":
                break
            time.sleep(0.05)
        assert result["state"] == "completed" and model.calls == 3


def test_busy_response_requires_manual_requeue_without_budget_reset(env):
    from procurepilot.models import ProviderError

    class Busy(FakeModel):
        def assess(self, *args):
            raise ProviderError(429, "1305")

    r = queued(env)
    jobs.run_once(env[1], Busy())
    model = FakeModel()
    assert not jobs.run_once(env[1], model)
    assert status(env, r["id"])["state"] == "waiting_provider"
    c, db, w = env
    assert c.post(f"/api/w/{w}/analysis-jobs/{r['id']}/resume").status_code == 409
    with db.transaction(w) as s:
        task = get(s, w, "analysis", r["id"])
        task.data = {
            **task.data,
            "retry_at": (jobs.utcnow() - timedelta(seconds=1)).isoformat(),
        }
    assert c.post(f"/api/w/{w}/analysis-jobs/{r['id']}/resume").status_code == 202
    jobs.run_once(db, model)
    done = status(env, r["id"])
    assert (
        done["tokens"] == 450 and done["attempts"] == 4 and done["max_tokens"] == 16000
    )


def test_explicit_response_continuation_runs_in_background(env):
    from procurepilot.models import ModelResponseError

    class Truncated(FakeModel):
        def assess(self, *args):
            raise ModelResponseError(
                "output_truncated",
                {"prompt_tokens": 100, "completion_tokens": 80},
                "length",
            )

    r = queued(env)
    jobs.run_once(env[1], Truncated())
    response = env[0].post(
        f"/api/w/{env[2]}/analysis-jobs/{r['id']}/continue", json={"seconds": 300}
    )
    assert response.status_code == 202 and response.json()["tokens"] == 180
    assert response.json()["state"] == "queued"
    model = FakeModel()
    jobs.run_once(env[1], model)
    assert status(env, r["id"])["tokens"] == 630 and model.calls == 3


def test_queue_limit_rejects_new_job_but_allows_idempotent_lookup(env):
    first = queued(env)
    for i in range(19):
        queued(env, f"capacity-{i:03d}")
    assert queued(env)["id"] == first["id"]
    with pytest.raises(HTTPException) as exc:
        queued(env, "capacity-overflow")
    assert exc.value.status_code == 429
