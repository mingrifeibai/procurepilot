"""将模型提出的缺口转为持久化问题；回答不能隐式修改硬约束。"""

from pydantic import Field
from .domain import StrictModel, Requirement
from .storage import add, records, view
from .service import get, latest, fail, audit, save_requirement


class Answer(StrictModel):
    answer: str = Field(min_length=1, max_length=2000)
    requirement: Requirement | None = None


def create_questions(session, wid, task, assessments):
    existing = {
        r.data["question"]
        for r in records(session, wid, "clarification")
        if r.data["analysis_id"] == task.id
    }
    for assessment in assessments:
        for question in assessment["missing_information"]:
            if question not in existing:
                add(
                    session,
                    wid,
                    "clarification",
                    {
                        "analysis_id": task.id,
                        "requirement_id": task.data["requirement_id"],
                        "question": question,
                        "state": "open",
                        "asked_by": assessment["role"],
                    },
                )
                existing.add(question)


def answer(session, wid, qid, body, actor):
    question = get(session, wid, "clarification", qid)
    if question.data["state"] == "answered":
        if question.data["answer"] == body.answer and body.requirement is None:
            return view(question)
        fail("此问题已回答，如需变更请创建新需求版本")
    req = latest(session, wid, "requirement")
    if req.id != question.data["requirement_id"]:
        fail("该问题绑定的需求已经变更，请基于最新需求分析")
    resolved_req = (
        save_requirement(session, wid, body.requirement, actor)["id"]
        if body.requirement
        else req.id
    )
    question.data = {
        **question.data,
        "state": "answered",
        "answer": body.answer,
        "actor": actor,
        "resolved_requirement_id": resolved_req,
    }
    audit(session, wid, actor, "clarification.answered", qid)
    return view(question)


def confirmed_answers(session, wid, requirement_id):
    return [
        {
            "id": "answer:" + r.id,
            "question": r.data["question"],
            "answer": r.data["answer"],
            "kind": "user_confirmation",
        }
        for r in records(session, wid, "clarification")
        if r.data["state"] == "answered"
        and r.data.get("resolved_requirement_id") == requirement_id
    ]
