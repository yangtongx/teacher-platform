from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

MAX_CHARS = 20_000
MAX_PDF_PAGES = 30
MAX_UNCOMPRESSED = 20 * 1024 * 1024
MAX_ZIP_ENTRIES = 200


def extract_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        text = _decode(data)
    elif suffix == ".docx":
        text = _docx(data)
    elif suffix == ".pdf":
        text = _pdf(data)
    else:
        raise ValueError("只支持 txt、md、docx、pdf")
    text = text.replace("\x00", "").strip()
    if not text:
        raise ValueError("这份文档里没有读出文字")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n…（后面省略）"
    return text


def safe_filename(filename: str) -> str:
    name = Path(str(filename or "")).name.strip().replace("\x00", "")
    if not name or name in {".", ".."}:
        raise ValueError("文件名不对")
    if len(name) > 80:
        name = name[:80]
    return name


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("这份文本不是 UTF-8 或 GB18030")


def _docx(data: bytes) -> str:
    _reject_zip(data)
    try:
        from docx import Document
    except ImportError as exc:
        raise ValueError("服务器还不能读 Word") from exc
    try:
        document = Document(BytesIO(data))
    except Exception as exc:
        raise ValueError("这份 Word 读不出来") from exc
    parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" ".join(cells))
    return "\n".join(parts)


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("服务器还不能读 PDF") from exc
    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        raise ValueError("这份 PDF 读不出来") from exc
    pages = []
    used = 0
    for index, page in enumerate(reader.pages):
        if index >= MAX_PDF_PAGES or used >= MAX_CHARS:
            pages.append("…（后面的页省略）")
            break
        text = page.extract_text() or ""
        pages.append(text)
        used += len(text)
    return "\n".join(pages)


def _reject_zip(data: bytes) -> None:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("这份 Word 读不出来") from exc
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ValueError("这份 Word 读不出来")
    total = sum(info.file_size for info in infos)
    if total > MAX_UNCOMPRESSED:
        raise ValueError("这份 Word 太大")
