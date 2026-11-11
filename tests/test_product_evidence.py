"""求解器使用的产品参数也必须给模型，目录证据与报价证据分别引用。"""

from .test_procurement import requirement
from .test_models import FakeModel
from procurepilot import analysis


def test_model_receives_actual_catalog_evidence(env):
    c, db, w = env
    requirement(c, w)

    class Inspect(FakeModel):
        def assess(self, role, context, max_output):
            dev = next(
                p for p in context["product_evidence"] if p["id"] == "catalog:dev-32"
            )
            assert dev["product"]["memory_gb"] == 32
            assert "usb-c" in dev["product"]["interfaces"]
            assert dev["id"] in context["evidence_ids"] and dev["simulated"]
            assert "cpu" not in dev["product"]  # 未提供的参数不能为了通过评测而补造。
            result = super().assess(role, context, max_output)
            result.assessment.evidence_ids = [dev["id"]]
            return result

    result = analysis.run(db, w, "test", Inspect())
    assert result["state"] == "completed"
