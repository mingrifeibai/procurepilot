"""模型选择专业任务及持久化恢复；委派不能绕过预算、证据和角色权限。"""

import pytest
from procurepilot import analysis
from procurepilot.models import Delegation
from .test_models import FakeModel
from .test_procurement import requirement


class Coordinator(FakeModel):
    def __init__(self, tasks):
        super().__init__()
        self.tasks = tasks
        self.roles = []

    def assess(self, role, context, max_output):
        self.roles.append(role)
        result = super().assess(role, context, max_output)
        if role == "采购协调员":
            result.assessment.delegations = self.tasks
        if role == "方案规划员":
            assert context["delegated_task"]["objective"] == "核对固定方案是否满足预算"
        return result


def test_model_selects_specialist_and_objective(env):
    requirement(env[0], env[2])
    model = Coordinator(
        [Delegation(role="方案规划员", objective="核对固定方案是否满足预算")]
    )
    done = analysis.run(env[1], env[2], "test", model, coordination_mode="dynamic")
    assert done["state"] == "completed" and done["tokens"] == 450
    assert model.roles == ["采购协调员", "方案规划员", "证据核验员"]
    assert done["delegation_state"] == "model_selected"


def test_explicit_empty_delegation_avoids_unnecessary_specialists(env):
    requirement(env[0], env[2])
    model = Coordinator([])
    done = analysis.run(env[1], env[2], "test", model, coordination_mode="dynamic")
    assert done["state"] == "completed" and model.roles == ["采购协调员", "证据核验员"]
    assert done["tokens"] == 300


def test_missing_delegation_records_fallback_honestly(env):
    requirement(env[0], env[2])
    model = Coordinator(None)
    done = analysis.run(env[1], env[2], "test", model, coordination_mode="dynamic")
    assert done["delegation_state"] == "fallback" and model.calls == 3


def test_checkpoint_retains_model_chosen_tasks_without_replanning(env):
    requirement(env[0], env[2])
    model = Coordinator(
        [Delegation(role="方案规划员", objective="核对固定方案是否满足预算")]
    )
    with pytest.raises(InterruptedError):
        analysis.run(
            env[1],
            env[2],
            "test",
            model,
            coordination_mode="dynamic",
            stop_after="采购协调员",
        )
    task = env[0].get(f"/api/w/{env[2]}/snapshot").json()["analysis"][-1]
    done = analysis.run(env[1], env[2], "test", model, task_id=task["id"])
    assert done["state"] == "completed" and model.roles.count("采购协调员") == 1
    assert done["tokens"] == 450


def test_duplicate_delegation_rejected_but_usage_counted(env):
    requirement(env[0], env[2])
    task = Delegation(role="寻源员", objective="核对产品参数")
    model = Coordinator([task, task])
    done = analysis.run(env[1], env[2], "test", model, coordination_mode="dynamic")
    assert (
        done["state"] == "waiting_response"
        and done["tokens"] == 150
        and model.calls == 1
    )


def test_fixed_mode_cannot_silently_start_model_delegation(env):
    requirement(env[0], env[2])
    model = Coordinator([Delegation(role="寻源员", objective="核对产品参数")])
    done = analysis.run(env[1], env[2], "test", model)
    assert done["state"] == "waiting_response" and model.calls == 1


def test_single_agent_baseline_has_one_call(env):
    requirement(env[0], env[2])
    model = Coordinator(None)
    done = analysis.run(env[1], env[2], "test", model, coordination_mode="single")
    assert done["state"] == "completed" and done["tokens"] == 150
    assert model.roles == ["采购协调员"]
