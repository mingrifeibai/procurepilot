"""本地CPU OCR的真实合成扫描件验收。没有外部模型调用。"""

import base64
import io
from pathlib import Path
import pytest
from fastapi import HTTPException
from procurepilot.evidence import Attachment, extract


@pytest.fixture
def scan_image():
    pytest.importorskip("rapidocr_onnxruntime")
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1400, 350), "white")
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = (
        ImageFont.truetype(str(font_path), 54)
        if font_path.exists()
        else ImageFont.load_default(size=54)
    )
    ImageDraw.Draw(image).text(
        (50, 80), "Price 5800   Memory 32GB   USB-C", font=font, fill="black"
    )
    yield image
    image.close()


@pytest.mark.parametrize(
    "format,filename", [("PNG", "quote.png"), ("PDF", "quote.pdf")]
)
def test_real_scan_extracts_amount_and_memory(scan_image, format, filename):
    output = io.BytesIO()
    scan_image.save(output, format=format)
    result = extract(
        Attachment(
            filename=filename,
            content_base64=base64.b64encode(output.getvalue()).decode(),
            ocr=True,
        )
    )
    assert "5800" in result["text"] and "32" in result["text"]
    assert result["ocr"]["requires_review"] and result["ocr"]["pages"] == 1
    assert all(0.5 <= line["confidence"] <= 1 for line in result["ocr"]["lines"])


def test_image_requires_explicit_ocr(scan_image):
    output = io.BytesIO()
    scan_image.save(output, format="PNG")
    with pytest.raises(HTTPException) as error:
        extract(
            Attachment(
                filename="quote.png",
                content_base64=base64.b64encode(output.getvalue()).decode(),
            )
        )
    assert error.value.status_code == 422
