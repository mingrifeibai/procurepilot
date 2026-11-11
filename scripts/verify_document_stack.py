"""真实OpenSearch、本机OCR、原件及OTLP验收。只使用隔离库与合成附件。"""

import base64
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from procurepilot.api import create_app
from procurepilot.observability import flush
from procurepilot.search_index import SearchIndex
from procurepilot.storage import Store


def run():
    index_name = "procurepilot-check-" + uuid4().hex
    os.environ.update(
        OPENSEARCH_INDEX=index_name,
        OPENSEARCH_URL="http://127.0.0.1:59200",
        SEARCH_BACKEND="opensearch",
        DEMO_MODE="1",
        MODEL_WORKER_MODE="embedded",
        REDIS_URL="",
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://127.0.0.1:24318/v1/traces",
    )
    index = SearchIndex()
    # 容器启动后仍需完成选主和分片恢复，预检通过后才写入验收数据。
    with httpx.Client(
        trust_env=False, timeout=2, headers={"Accept-Encoding": "identity"}
    ) as client:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                health = (
                    client.get(index.url + "/_cluster/health").raise_for_status().json()
                )
                client.get("http://127.0.0.1:26687/api/services").raise_for_status()
                if health["status"] in ("green", "yellow"):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("OpenSearch或Jaeger尚未就绪")
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = (
        ImageFont.truetype(str(font_path), 54)
        if font_path.exists()
        else ImageFont.load_default(size=54)
    )
    buffer = io.BytesIO()
    with Image.new("RGB", (1400, 350), "white") as image:
        ImageDraw.Draw(image).text(
            (50, 80),
            "Dock USB-C   Price 450   Warranty 3 years",
            font=font,
            fill="black",
        )
        image.save(buffer, format="PNG")
    raw = buffer.getvalue()
    try:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            os.environ["OBJECT_ROOT"] = str(path / "objects")
            store = Store("sqlite:///" + (path / "test.db").as_posix())
            try:
                with TestClient(create_app(store)) as client:
                    client.headers["Authorization"] = "Bearer demo-buyer"
                    wid = (
                        client.post("/api/workspaces", json={"name": "合成验收资料"})
                        .raise_for_status()
                        .json()["id"]
                    )
                    prefix = f"/api/w/{wid}"
                    uploaded = client.post(
                        prefix + "/attachments",
                        json={
                            "filename": "报价扫描件.png",
                            "ocr": True,
                            "content_base64": base64.b64encode(raw).decode(),
                        },
                    ).raise_for_status()
                    aid = uploaded.json()["id"]
                    assert "450" in uploaded.json()["preview"]
                    assert (
                        client.get(prefix + f"/attachments/{aid}/original").content
                        == raw
                    )
                    assert (
                        client.get(
                            prefix + "/evidence/search", params={"query": "Warranty"}
                        ).status_code
                        == 409
                    )
                    job = (
                        client.post(prefix + "/evidence/index", json={})
                        .raise_for_status()
                        .json()
                    )
                    assert (
                        client.post(prefix + "/evidence/index", json={}).json()["id"]
                        == job["id"]
                    )
                    deadline = time.monotonic() + 40
                    while time.monotonic() < deadline:
                        status = (
                            client.get(prefix + "/evidence/index")
                            .raise_for_status()
                            .json()[-1]
                        )
                        if status["state"] in ("ready", "failed", "stale"):
                            break
                        time.sleep(0.5)
                    assert status["state"] == "ready", status
                    response = client.get(
                        prefix + "/evidence/search", params={"query": "Warranty USB-C"}
                    ).raise_for_status()
                    hits = response.json()
                    assert hits[0]["artifact_id"] == aid and hits[0][
                        "method"
                    ].startswith("opensearch_")
                    assert "Warranty" in hits[0]["snippet"]
                    trace = response.headers["X-Trace-Id"]
                    other = client.post(
                        "/api/workspaces", json={"name": "隔离验收"}
                    ).json()["id"]
                    assert (
                        client.get(
                            f"/api/w/{other}/attachments/{aid}/original"
                        ).status_code
                        == 404
                    )
                    assert not client.get(prefix + "/snapshot").json()["order"]
            finally:
                store.engine.dispose()
        assert flush()
        with httpx.Client(
            base_url="http://127.0.0.1:26687", trust_env=False, timeout=4
        ) as collector:
            for _ in range(12):
                result = collector.get(f"/api/traces/{trace}")
                if result.is_success and result.json().get("data"):
                    break
                time.sleep(0.5)
            else:
                raise AssertionError("未在Jaeger查到OTLP追踪")
            assert "Warranty USB-C" not in result.text
            assert "demo-buyer" not in result.text
        return {
            "passed": True,
            "index_chunks": status["total_chunks"],
            "ocr": "local CPU",
            "original_bytes_verified": len(raw),
            "source_matches": True,
            "isolation_checked": True,
            "trace_id": trace,
            "orders": 0,
            "external_model_calls": 0,
        }
    finally:
        original_error = sys.exc_info()[0]
        try:
            if index.request("HEAD", "/" + index_name) is not None:
                index.request("DELETE", "/" + index_name)
        except Exception:
            if original_error is None:
                raise
            print(f"清理验收索引失败，可在引擎恢复后清理 {index_name}", file=sys.stderr)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run()
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
