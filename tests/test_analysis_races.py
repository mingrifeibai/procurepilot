"""远端调用期间发生变更或取消时，最终状态仍由数据库裁决。"""

from .test_procurement import requirement
from .test_models import FakeModel
from procurepilot import analysis
from procurepilot.storage import records


def test_quote_change_during_final_remote_call_marks_stale(env):
    c, db, w = env
    requirement(c, w)

    class Changing(FakeModel):
        def assess(self, role, context, max_output):
            if role == "证据核验员":
                with db.transaction(w) as s:
                    q = records(s, w, "quote")[0]
                    q.data = {**q.data, "stock": 1}
            return super().assess(role, context, max_output)

    assert analysis.run(db, w, "test", Changing())["state"] == "stale"


def test_cancelling_active_remote_request_stops_later_roles(env):
    c, db, w = env
    requirement(c, w)

    class Cancelling(FakeModel):
        def assess(self, role, context, max_output):
            with db.transaction(w) as s:
                aid = records(s, w, "analysis")[-1].id
            assert c.post(f"/api/w/{w}/analyses/{aid}/cancel").status_code == 200
            return super().assess(role, context, max_output)

    model = Cancelling()
    r = analysis.run(db, w, "test", model)
    assert r["state"] == "cancelled" and model.calls == 1
    assert r["tokens"] == 150  # 已发生的远端调用仍计入用量。
