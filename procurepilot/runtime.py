"""有界 LangGraph 运行。每个节点提交结构化快照，重启后从未完成节点恢复。

当前为离线确定性角色节点，不调用大模型，不产生虚构 Token 或费用。
数据库快照是恢复依据；模型适配层后续接入不应改变业务审批边界。
"""

from typing import TypedDict
from langgraph.graph import StateGraph, END
from .storage import add, view, records
from .service import get, latest, plan, audit


class RunState(TypedDict):
    run_id: str


STAGES = ("clarify", "source", "plan", "verify")


def start(session, wid, actor, max_steps=8):
    requirement = latest(session, wid, "requirement")
    return view(
        add(
            session,
            wid,
            "run",
            {
                "requirement_id": requirement.id,
                "state": "pending",
                "completed": [],
                "max_steps": max_steps,
                "steps": 0,
                "actor": actor,
                "mode": "offline_rules",
                "tokens": 0,
                "cost": "0",
                "tool_calls": 0,
            },
        )
    )


def resume(store, wid, rid, stop_after=None):
    """节点独立事务；故障仅回滚当前节点。恢复不重复创建已提交的方案。"""
    graph = StateGraph(RunState)

    def node(stage):
        def invoke(state):
            with store.transaction(wid) as session:
                item = get(session, wid, "run", rid)
                data = dict(item.data)
                if stage in data["completed"] or data["state"] in (
                    "completed",
                    "evidence_insufficient",
                    "budget_exhausted",
                    "stale",
                ):
                    return state
                if latest(session, wid, "requirement").id != data["requirement_id"]:
                    item.data = {**data, "state": "stale"}
                    return state
                if data["steps"] >= data["max_steps"]:
                    item.data = {**data, "state": "budget_exhausted"}
                    return state
                result = {}
                if stage == "clarify":
                    result = {"requirement_id": data["requirement_id"], "missing": []}
                elif stage == "source":
                    from .governance import read

                    quotes = [view(q) for q in records(session, wid, "quote")]
                    result, hit = read(
                        session,
                        wid,
                        data["actor"],
                        rid,
                        "source_quotes",
                        {},
                        quotes,
                        lambda: {"quote_ids": [q["id"] for q in quotes]},
                    )
                    data["tool_calls"] += int(not hit)
                    data["cache_hits"] = data.get("cache_hits", 0) + int(hit)
                elif stage == "plan":
                    result = plan(session, wid, data["actor"])
                    data["plan_id"] = result["id"]
                    data["tool_calls"] += 1
                elif stage == "verify":
                    result = get(session, wid, "plan", data["plan_id"]).data
                    data["state"] = (
                        "completed"
                        if result["status"] == "feasible"
                        else "evidence_insufficient"
                    )
                data.update(
                    steps=data["steps"] + 1, completed=[*data["completed"], stage]
                )
                if data["state"] == "pending":
                    data["state"] = "running"
                item.data = data
                add(
                    session,
                    wid,
                    "trace",
                    {"run_id": rid, "stage": stage, "result": result},
                )
                audit(session, wid, data["actor"], f"run.{stage}", rid)
            if stage == stop_after:
                raise InterruptedError("故障注入：节点提交后中断，可从检查点恢复")
            return state

        return invoke

    for stage in STAGES:
        graph.add_node(stage, node(stage))
    graph.set_entry_point(STAGES[0])
    for before, after in zip(STAGES, STAGES[1:]):
        graph.add_edge(before, after)
    graph.add_edge(STAGES[-1], END)
    graph.compile().invoke({"run_id": rid})
    with store.transaction(wid) as session:
        return view(get(session, wid, "run", rid))
