"""多词召回、长文后部证据、原文偏移、隔离和模型证据接入。"""

import base64
import pytest
from fastapi import HTTPException
from procurepilot.retrieval import search
from procurepilot import analysis
from .test_models import FakeModel
from .test_procurement import requirement


def artifact(aid, text):
    return {"id": aid, "filename": aid + ".txt", "text": text}


def upload(c, w, text):
    return c.post(
        f"/api/w/{w}/attachments",
        json={
            "filename": "evidence.txt",
            "content_base64": base64.b64encode(text.encode()).decode(),
        },
    ).json()["id"]


def test_multiple_separated_terms_and_source_offsets():
    docs = [
        artifact("irrelevant", "显示器 HDMI 价格1800"),
        artifact("target", "扩展坞支持 USB-C 接口，七天交付，含税报价450元。"),
    ]
    hits = search(docs, "扩展坞 USB-C 报价")
    assert hits[0]["artifact_id"] == "target"
    for hit in hits:
        text = next(d["text"] for d in docs if d["id"] == hit["artifact_id"])
        assert text[hit["offset"] : hit["end_offset"]] == hit["snippet"]
        assert hit["untrusted"] and len(hit["content_sha256"]) == 64


def test_late_evidence_and_casefold_preserve_original_offsets():
    text = "背景说明。" * 800 + "\nUSB-C 扩展坞设备的质保为三年，七天交付。"
    hits = search([artifact("long", text)], "usb-c 扩展坞")
    assert hits[0]["offset"] > 3000 and "质保为三年" in hits[0]["snippet"]
    unicode_hits = search([artifact("unicode", "Straße USB-C 接口")], "STRASSE")
    assert unicode_hits[0]["offset"] == 0 and unicode_hits[0]["snippet"].startswith(
        "Straße"
    )


@pytest.mark.parametrize("query", ["", "  ", "!?", "x" * 201])
def test_bad_query_rejected(query):
    with pytest.raises(HTTPException):
        search([], query)


def test_no_match_limit_and_capacity():
    assert search([artifact("a", "产品电脑")], "no-such-model") == []
    assert (
        len(search([artifact(str(i), "电脑 USB-C") for i in range(30)], "USB-C", 4))
        == 4
    )
    with pytest.raises(HTTPException):
        search([artifact("huge", "a" * 2_000_001)], "a")


def test_api_isolation_and_retrieved_chunks_used_by_analysis(env):
    c, db, w = env
    requirement(c, w)
    text = "无关背景。" * 500 + "电脑 USB-C 内存32GB，条款：三年保修。"
    aid = upload(c, w, text)
    other = c.post("/api/workspaces", json={"name": "隔离工作区"}).json()["id"]
    assert (
        c.get(f"/api/w/{other}/evidence/search", params={"query": "USB-C"}).json() == []
    )

    class Inspect(FakeModel):
        def assess(self, role, context, max_output):
            assert any("三年保修" in a["preview"] for a in context["artifacts"])
            assert all(
                a["id"] in context["evidence_ids"] and a["untrusted"]
                for a in context["artifacts"]
            )
            assert any(
                f"artifact:{aid}:offset:" in a["id"] for a in context["artifacts"]
            )
            assert all(
                text[a["offset"] : a["offset"] + len(a["preview"])] == a["preview"]
                for a in context["artifacts"]
            )
            return super().assess(role, context, max_output)

    done = analysis.run(db, w, "test", Inspect(), max_tokens=32000)
    assert done["state"] == "completed" and len(done["assessments"]) == 5
    full = analysis.run(
        db, w, "test", Inspect(), max_tokens=32000, context_excerpt="full_chunk"
    )
    assert full["state"] == "completed"
    assert sum(row["bytes"] for row in done["evidence_ranges"].values()) < sum(
        row["bytes"] for row in full["evidence_ranges"].values()
    )


def test_new_attachment_invalidates_queued_context(env):
    from procurepilot import jobs

    c, db, w = env
    requirement(c, w)
    task = analysis.run(db, w, "test", prepare_only=True)
    upload(c, w, "新增电脑 USB-C 配置证据")
    model = FakeModel()
    jobs.run_once(db, model)
    result = c.get(f"/api/w/{w}/analyses/{task['id']}").json()
    assert result["state"] == "stale" and model.calls == 0
