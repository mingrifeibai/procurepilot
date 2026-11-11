"""本机CPU扫描件识别。OCR结果带置信度和页号，作为待核对证据保留。"""

import io
import math
from functools import lru_cache
from threading import Lock
import warnings
from .service import fail

_lock = Lock()


@lru_cache(maxsize=1)
def engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR(
        intra_op_num_threads=2,
        inter_op_num_threads=1,
        det_use_cuda=False,
        cls_use_cuda=False,
        rec_use_cuda=False,
    )


def extract(raw, extension):
    """最多5页，逐页释放图像；限制像素及推理尺寸，避免大图挤占内存。"""
    from PIL import Image
    import numpy as np
    from fastapi import HTTPException

    lines = []
    pages = 0

    def recognize(image, page):
        if image.width * image.height > 12_000_000:
            fail("OCR图片最多1200万像素，请缩小后上传", 413)
        image.thumbnail((2400, 2400))
        with _lock:
            result, _ = engine()(np.asarray(image.convert("RGB")))
        for row in result or []:
            score = float(row[2])
            if math.isfinite(score) and 0.5 <= score <= 1 and row[1].strip():
                lines.append(
                    {"page": page, "text": row[1], "confidence": round(score, 4)}
                )

    try:
        if extension == "pdf":
            import pypdfium2 as pdfium

            with pdfium.PdfDocument(raw) as document:
                if len(document) > 5:
                    fail("扫描PDF一次最多5页，请拆分后上传", 413)
                pages = len(document)
                for i in range(pages):
                    page = document[i]
                    try:
                        width, height = page.get_size()
                        scale = min(2, 2400 / max(width, 1), 2400 / max(height, 1))
                        bitmap = page.render(scale=scale)
                        try:
                            image = bitmap.to_pil()
                            recognize(image, i + 1)
                            image.close()
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as image:
                    recognize(image, 1)
                    pages = 1
    except HTTPException:
        raise
    except ImportError:
        fail("OCR依赖尚未安装，请安装OCR依赖后重试", 503)
    except Exception:
        fail("扫描件无法识别，请检查文件格式或图像质量", 422)
    if not lines:
        fail("未识别到足够清晰的文字，请提高扫描清晰度", 422)
    text = "\n".join(f"[OCR page {line['page']}] {line['text']}" for line in lines)
    return {
        "text": text,
        "ocr": {
            "engine": "rapidocr-onnxruntime-1.4.4",
            "pages": pages,
            "lines": lines,
            "requires_review": True,
        },
    }
