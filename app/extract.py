"""从老师上传的 txt、md、docx、pdf 和图片抽出纯文字。正文只当资料，不当命令。

PDF 先读文字层。没有文字的页，以及 jpg、png，都交给 image_ocr：
先分版面，中文用 RapidOCR，没认准的英文行再用 TrOCR。
识别结果会标明需要核对。
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

MAX_CHARS = 20_000
MAX_PDF_PAGES = 30
MAX_OCR_PAGES = 10
MIN_PAGE_CHARS = 12
MAX_UNCOMPRESSED = 20 * 1024 * 1024
MAX_ZIP_ENTRIES = 200
OCR_NOTE = "以下内容来自图片或扫描页识别，请核对。"


def extract_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        text = _decode(data)
    elif suffix == ".docx":
        text = _docx(data)
    elif suffix == ".pdf":
        text = _pdf(data)
    elif suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        text = _image_file(data)
    else:
        raise ValueError("只支持 txt、md、docx、pdf、jpg、png、webp")
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
    ocr_pages = 0
    used_ocr = False
    skipped = False
    holder: dict = {"doc": None}
    try:
        for index, page in enumerate(reader.pages):
            if index >= MAX_PDF_PAGES or used >= MAX_CHARS:
                pages.append("…（后面的页省略）")
                break
            text = (page.extract_text() or "").strip()
            if len(text) < MIN_PAGE_CHARS:
                if ocr_pages >= MAX_OCR_PAGES:
                    skipped = True
                    continue
                text = _ocr_page(data, index, holder).strip()
                ocr_pages += 1
                if text:
                    used_ocr = True
            if not text:
                continue
            pages.append(text)
            used += len(text)
    finally:
        doc = holder.get("doc")
        if doc is not None:
            doc.close()
    if skipped:
        pages.append(f"…（扫描页只识别了前 {MAX_OCR_PAGES} 页）")
    content = pages[:-1] if skipped else pages
    if not any(part.strip() for part in content):
        if ocr_pages:
            raise ValueError("这份扫描件没有识别出文字")
        raise ValueError("这份文档里没有读出文字")
    body = "\n".join(pages).strip()
    if used_ocr:
        body = OCR_NOTE + "\n" + body
    return body


def _ocr_page(data: bytes, index: int, holder: dict) -> str:
    if holder.get("doc") is None:
        holder["doc"] = _open_pdf(data)
    image = _page_image(holder["doc"], index)
    return _recognize_image(image)


def _open_pdf(data: bytes):
    try:
        import pymupdf
    except ImportError as exc:
        raise ValueError("这份 PDF 是扫描件，服务器还不能识别图片里的文字") from exc
    try:
        return pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError("这份扫描件读不出来") from exc


def _page_image(doc, index: int):
    import pymupdf

    page = doc.load_page(index)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6), alpha=False)
    try:
        import numpy as np

        return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n).copy()
    except Exception:
        return pix.tobytes("png")


def _image_file(data: bytes) -> str:
    from app.image_ocr import read_bytes

    text = read_bytes(data).strip()
    if not text:
        raise ValueError("这张图片没有识别出文字")
    return OCR_NOTE + "\n" + text


def _recognize_image(image) -> str:
    from app.image_ocr import read_image

    array = image
    if getattr(image, "ndim", 0) == 3 and image.shape[2] == 3:
        array = image[:, :, ::-1]
    try:
        return read_image(array)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("这张图片没有识别出来") from exc


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
