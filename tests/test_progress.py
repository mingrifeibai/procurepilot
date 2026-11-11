"""工作区分析 SSE 游标、授权和恢复回读。"""

from procurepilot import progress
from procurepilot.storage import add


def test_analysis_progress_cursor_reconnects_to_latest_workspace_state(
    env, monkeypatch
):
    client, store, wid = env
    monkeypatch.setattr(progress, "WINDOW_SECONDS", 0)
    path = f"/api/w/{wid}/analysis-events"
    first = client.get(path)
    assert first.headers["content-type"].startswith("text/event-stream")
    assert first.headers["x-accel-buffering"] == "no"
    assert "event: analysis_changed" in first.text
    cursor = first.text.splitlines()[0].removeprefix("id: ")

    with store.transaction(wid) as session:
        add(session, "other-workspace", "analysis", {"state": "running"})
    assert client.get(path, headers={"Last-Event-ID": cursor}).text == ": heartbeat\n\n"
    with store.transaction(wid) as session:
        item = add(
            session, wid, "analysis", {"state": "queued", "steps": 0, "tokens": 0}
        )
    queued = client.get(path, headers={"Last-Event-ID": cursor}).text
    assert "event: analysis_changed" in queued
    queued_cursor = queued.splitlines()[0].removeprefix("id: ")
    assert queued_cursor != cursor
    assert (
        client.get(path, headers={"Last-Event-ID": queued_cursor}).text
        == ": heartbeat\n\n"
    )
    with store.transaction(wid) as session:
        session.get(type(item), item.id).data = {
            "state": "completed",
            "steps": 4,
            "tokens": 900,
        }
    completed = client.get(path, headers={"Last-Event-ID": queued_cursor}).text
    assert "event: analysis_changed" in completed and queued_cursor not in completed
    assert "completed" not in completed and wid not in completed
    assert (
        client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401
    )
    assert client.get("/api/w/missing/analysis-events").status_code == 404
