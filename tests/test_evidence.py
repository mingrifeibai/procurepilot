"""附件隔离、分页上限、来源及压缩约束保留。"""

import base64
import pytest
from .test_procurement import requirement


def test_quote_import_validates_batch_and_deduplicates_source(env):
    c, _, w = env
    csv_text = 'product_id,supplier,unit_price,stock,delivery_days\n"dev-32","样例\n供应商",5100,12,6\ninvalid,无产品,100,1,2\n'
    attachment = c.post(
        f"/api/w/{w}/attachments",
        json={
            "filename": "quotes.csv",
            "content_base64": base64.b64encode(csv_text.encode()).decode(),
        },
    ).json()
    prefix = f"/api/w/{w}/attachments/{attachment['id']}"
    rows = c.get(prefix + "/quotes-preview").json()
    assert [row["row"] for row in rows] == [2, 4]
    assert rows[0]["normalized"]["unit_price"] == "5100" and rows[1]["errors"]
    before = c.get(f"/api/w/{w}/snapshot").json()["quote"]
    assert c.post(prefix + "/quotes-import", json={"rows": [2, 4]}).status_code == 422
    assert c.get(f"/api/w/{w}/snapshot").json()["quote"] == before
    result = c.post(prefix + "/quotes-import", json={"rows": [2]}).json()
    assert result[0]["price_cents"] == 510000 and result[0]["source"].endswith(":row:2")
    assert c.post(prefix + "/quotes-import", json={"rows": [2, 2]}).json() == result
    assert len(c.get(f"/api/w/{w}/snapshot").json()["quote"]) == len(before) + 1
    denied = c.post(
        prefix + "/quotes-import",
        json={"rows": [2]},
        headers={"Authorization": "Bearer demo-viewer"},
    )
    assert denied.status_code == 403


def test_attachment_search_preview_and_pagination(env):
    c, _, w = env
    text = "product_id,supplier,unit_price,stock,delivery_days\ndev-32,供应商 B,5600,10,7\n"
    r = c.post(
        f"/api/w/{w}/attachments",
        json={
            "filename": "quote.csv",
            "content_base64": base64.b64encode(text.encode()).decode(),
        },
    )
    assert r.status_code == 201
    aid = r.json()["id"]
    preview = c.get(f"/api/w/{w}/attachments/{aid}/quotes-preview").json()
    assert preview[0]["missing"] == [] and f"artifact:{aid}" in preview[0]["source"]
    assert (
        c.get(f"/api/w/{w}/evidence/search", params={"query": "供应商"}).json()[0][
            "artifact_id"
        ]
        == aid
    )
    assert (
        len(c.get(f"/api/w/{w}/attachments/{aid}", params={"limit": 10}).json()["text"])
        == 10
    )
    assert (
        c.get(f"/api/w/{w}/attachments/{aid}", params={"limit": 10001}).status_code
        == 422
    )
    requirement(c, w)
    packet = c.get(f"/api/w/{w}/context").json()
    assert packet["requirement"]["budget_cents"] == 18000000
    assert packet["evidence_index"][0]["id"] == aid


@pytest.mark.parametrize(
    "filename,encoded",
    [("bad.exe", "aGVsbG8="), ("bad.txt", "%%%"), ("bad.xlsx", "aGVsbG8=")],
)
def test_attachment_validation(env, filename, encoded):
    c, _, w = env
    assert (
        c.post(
            f"/api/w/{w}/attachments",
            json={"filename": filename, "content_base64": encoded},
        ).status_code
        == 422
    )


def test_unconfirmed_preference_is_rejected(env):
    c, _, w = env
    assert (
        c.post(
            f"/api/w/{w}/preferences",
            json={"key": "brand", "value": "A", "confirmed": False},
        ).status_code
        == 422
    )
