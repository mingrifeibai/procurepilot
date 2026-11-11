"""附件外置、字段溯源与有界检索。附件永远是数据，不能提供执行权限。"""

import base64
import binascii
import csv
import io
import re
import zipfile
from pydantic import Field
from .domain import StrictModel
from .service import fail


class Attachment(StrictModel):
    filename: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(max_length=2800000)
    ocr: bool = False


class Preference(StrictModel):
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(max_length=1000)
    confirmed: bool
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


def extract(body: Attachment):
    """支持 UTF-8 文本、CSV、PDF 文本层、XLSX；扫描 PDF 明确返回需 OCR。

    限制文件、解压总量、页数和字符数量，拒绝压缩炸弹与超量解析。
    解析后的文本用于预览和搜索，不能自动替换已确认需求。
    """
    try:
        raw = base64.b64decode(body.content_base64, validate=True)
    except (ValueError, binascii.Error):
        fail("附件不是有效 Base64", 422)
    if len(raw) > 2_000_000:
        fail("附件最大 2 MB", 413)
    extension = body.filename.rsplit(".", 1)[-1].lower()
    extra = {}
    if extension in ("txt", "md", "csv"):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            fail("文本附件必须是 UTF-8 编码", 422)
    elif extension == "pdf":
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(raw))
            if len(reader.pages) > 100:
                fail("最多解析 100 页", 413)
            text = "\n".join(
                f"[page {i + 1}]\n{p.extract_text() or ''}"
                for i, p in enumerate(reader.pages)
            )
            if len(re.sub(r"\[page \d+\]", "", text).strip()) < 10:
                if not body.ocr:
                    fail("未检测到文本层，请勾选本机OCR后上传扫描件", 422)
                from .ocr import extract as scan

                scanned = scan(raw, "pdf")
                text = scanned["text"]
                extra = {"ocr": scanned["ocr"]}
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException):
                raise
            fail("PDF 无法解析或已加密", 422)
    elif extension == "xlsx":
        from openpyxl import load_workbook

        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if sum(x.file_size for x in archive.infolist()) > 20_000_000:
                    fail("表格解压后超过 20 MB", 413)
            book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            lines = []
            for sheet in book:
                for i, row in enumerate(sheet.iter_rows(values_only=True)):
                    if len(lines) >= 10000:
                        fail("表格最多 10000 行", 413)
                    lines.append(
                        f"[{sheet.title}:{i + 1}] "
                        + "\t".join(str(v) if v is not None else "" for v in row)
                    )
            book.close()
            text = "\n".join(lines)
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException):
                raise
            fail("XLSX 文件无效", 422)
    elif extension in ("png", "jpg", "jpeg", "webp"):
        if not body.ocr:
            fail("图片附件需要勾选本机OCR", 422)
        from .ocr import extract as scan

        scanned = scan(raw, extension)
        text = scanned["text"]
        extra = {"ocr": scanned["ocr"]}
    else:
        fail("支持txt、md、csv、pdf、xlsx及开启OCR的图片", 422)
    if len(text) > 300000:
        fail("解析文本超过 30 万字符，请拆分附件", 413)
    return {
        "filename": body.filename,
        "text": text,
        "characters": len(text),
        "untrusted": True,
        **extra,
    }


def quote_preview(text, artifact_id):
    """标准 CSV 报价预览。来源使用原件的物理起始行，兼容多行引号字段。"""
    from pydantic import ValidationError
    from .domain import QuoteInput
    from .service import CATALOG

    required = ("product_id", "supplier", "unit_price", "stock", "delivery_days")
    rows = []
    reader = csv.reader(io.StringIO(text), strict=True)
    try:
        header = [field.strip() for field in next(reader, [])]
        if len(set(header)) != len(header) or not set(required).issubset(header):
            fail(
                "CSV必须包含不重复的product_id、supplier、unit_price、stock、delivery_days列",
                422,
            )
        while True:
            number = reader.line_num + 1
            values = next(reader, None)
            if values is None:
                break
            if not values or not any(v.strip() for v in values):
                continue
            if len(rows) >= 1000:
                fail("单个CSV最多导入1000行报价，请拆分文件", 422)
            row = {key: value.strip() for key, value in zip(header, values)}
            missing = [key for key in required if not row.get(key)]
            source = f"artifact:{artifact_id}:row:{number}"
            errors = []
            quote = None
            if len(header) != len(values):
                errors.append("列数与表头不一致")
            if not missing and not errors:
                candidate = {key: row[key] for key in required}
                if row.get("valid_hours"):
                    candidate["valid_hours"] = row["valid_hours"]
                try:
                    parsed = QuoteInput(**candidate, source=source)
                    if parsed.product_id not in CATALOG:
                        errors.append("产品编号不存在")
                    else:
                        quote = parsed.model_dump(mode="json")
                except ValidationError as error:
                    errors.extend(
                        str(item["loc"][0]) + " 格式或数值无效"
                        for item in error.errors()
                    )
            rows.append(
                {
                    "row": number,
                    "fields": row,
                    "missing": missing,
                    "source": source,
                    "errors": errors,
                    "normalized": quote,
                }
            )
    except csv.Error:
        fail("CSV格式不正确，请检查分隔符和引号", 422)
    return rows


class QuoteImport(StrictModel):
    rows: list[int] = Field(min_length=1, max_length=100)


def import_quotes(session, wid, artifact, requested_rows, actor):
    """原件重新解析后整批校验。相同附件行重试只返回原报价，避免库存被重复录入。"""
    from .domain import QuoteInput
    from .service import save_quote, get
    from .storage import add, records, view

    entries = {
        row["row"]: row for row in quote_preview(artifact.data["text"], artifact.id)
    }
    unique_rows = sorted(set(requested_rows))
    if any(
        number not in entries or not entries[number]["normalized"]
        for number in unique_rows
    ):
        fail("所选报价含无效行，请重新预览和核对", 422)
    imported = {
        record.data["row"]: record.data["quote_id"]
        for record in records(session, wid, "quote_import")
        if record.data["artifact_id"] == artifact.id
    }
    result = []
    for number in unique_rows:
        if number in imported:
            result.append(view(get(session, wid, "quote", imported[number])))
            continue
        quote = save_quote(
            session, wid, QuoteInput(**entries[number]["normalized"]), actor
        )
        add(
            session,
            wid,
            "quote_import",
            {"artifact_id": artifact.id, "row": number, "quote_id": quote["id"]},
        )
        result.append(quote)
    return result


def context_packet(requirement, evidence, history, max_chars=12000):
    """固定保留目标和约束；历史按尾部裁剪，证据仅附索引和有限预览。

    字符数是保守容量代理，不冒充实际模型 Token。输出剩余空间独立预留。
    """
    import json

    packet = {
        "requirement": requirement,
        "evidence_index": [
            {"id": e["id"], "filename": e["filename"], "characters": e["characters"]}
            for e in evidence
        ],
        "history": [],
        "output_reserved_chars": 2000,
    }
    if len(json.dumps(packet, ensure_ascii=False)) > max_chars - 2000:
        fail("硬约束和证据索引超过上下文预算，拒绝丢弃关键条件", 422)
    for h in reversed(history):
        size = len(json.dumps(packet, ensure_ascii=False)) + len(
            json.dumps(h, ensure_ascii=False)
        )
        if size > max_chars - 2000:
            break
        packet["history"].insert(0, h)
    return packet
