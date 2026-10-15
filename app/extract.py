"""从老师上传的 txt、md、docx、pdf 和图片抽出纯文字。正文只当资料，不当命令。

PDF 先读文字层。没有文字的页，以及 jpg、png，都交给 image_ocr：
先分版面，中文用 RapidOCR，没认准的英文行再用 TrOCR。
识别结果会标明需要核对。
"""

from __future__ import annotations

import re
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
GRADE_MARK = "【成绩表】"
TABLE_MARK = "【表格】"
_SPREADSHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_table(filename: str, data: bytes) -> str:
    """读 csv / xlsx。成绩表会打上标记，分类时只看文件名，不看单元格。"""
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        grid = _decode(data)
    elif suffix in {".xlsx", ".xlsm"}:
        grid = _xlsx(data)
    elif suffix == ".xls":
        raise ValueError("请把表格另存为 xlsx 后再上传")
    else:
        raise ValueError("只支持 csv 和 xlsx")
    grid = grid.replace("\x00", "").strip()
    if not grid:
        raise ValueError("这份表格里没有读出内容")
    if len(grid) > MAX_CHARS:
        grid = grid[:MAX_CHARS] + "\n…（后面省略）"
    mark = GRADE_MARK if _looks_like_grades(grid) else TABLE_MARK
    return mark + "\n" + grid


def _looks_like_grades(text: str) -> bool:
    head = text[:500]
    named = "姓名" in head or "学生" in head
    scored = any(word in head for word in ("分数", "成绩", "得分", "总分", "语文", "数学", "英语"))
    return named and scored


def _looks_like_outline(text: str) -> bool:
    if _looks_like_grades(text):
        return False
    head = text[:800]
    return any(word in head for word in ("考试范围", "考试大纲", "大纲", "知识点", "双向细目", "考查", "题型", "命题"))


def brief_upload(filename: str, text: str) -> dict:
    """按正文判断附件，不看文件名。文件名可能只是日期或 IMG。摘要里不放学生姓名和分数。"""
    name = str(filename or "").replace("\n", " ").strip()
    raw = str(text or "")
    body = raw
    for mark in (GRADE_MARK, TABLE_MARK, OCR_NOTE):
        if body.startswith(mark):
            body = body[len(mark) :].lstrip("\n")
    photo = raw.startswith(OCR_NOTE)
    if raw.startswith(GRADE_MARK) or _looks_like_grades(body):
        kind, hint = "成绩表", _shape_hint(body, "表头像成绩，有姓名和分数")
    elif _looks_like_questions(body):
        where = "照片或扫描识别出的题目" if photo else "正文是题目"
        kind, hint = "题目", f"{where}，需核对，约 {_question_count(body)} 道"
    elif _looks_like_outline(body):
        kind, hint = "大纲", "内容在讲考试范围或知识点"
    elif raw.startswith(TABLE_MARK):
        kind, hint = "表格", _shape_hint(body, "普通表格")
    elif photo:
        kind, hint = "照片", "图片识别的文字，需核对"
    else:
        kind, hint = "文档", _first_line_hint(body)
    return {"filename": name, "kind": kind, "hint": hint}


def _looks_like_questions(text: str) -> bool:
    if _looks_like_grades(text):
        return False
    numbered = re.search(r"(?m)^\s*\d{1,2}[.、．]", text)
    choices = re.search(r"[AＡ][.、．\s].{0,80}[BＢ][.、．\s]", text)
    if numbered and choices:
        return True
    if re.search(r"选择题|填空题|判断题|解答题|计算题", text):
        return True
    if numbered and ("____" in text or "___" in text or "（ ）" in text or "( )" in text):
        return True
    return False


def _question_count(text: str) -> int:
    found = re.findall(r"(?m)^\s*\d{1,2}[.、．]", text)
    return max(1, len(found))


def _shape_hint(body: str, label: str) -> str:
    rows = [line for line in body.splitlines() if line.strip() and not line.startswith("…")]
    return f"{label}，约 {len(rows)} 行"


def _first_line_hint(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("【"):
            return line[:40]
    return "有正文"


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


def _xlsx(data: bytes) -> str:
    import xml.etree.ElementTree as ET

    _reject_zip(data, "这份表格读不出来")
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("这份表格读不出来") from exc
    with archive:
        names = archive.namelist()
        shared = _shared_strings(archive, names)
        sheets = sorted(name for name in names if name.startswith("xl/worksheets/sheet"))
        if not sheets:
            raise ValueError("这份表格里没有读出内容")
        try:
            root = ET.fromstring(archive.read(sheets[0]))
        except ET.ParseError as exc:
            raise ValueError("这份表格读不出来") from exc
    lines = []
    for row in root.iter(_SPREADSHEET_NS + "row"):
        cells = []
        for cell in row.findall(_SPREADSHEET_NS + "c"):
            value = _cell_value(cell, shared).strip()
            if value:
                cells.append(value)
        if cells:
            lines.append("\t".join(cells))
        if len(lines) >= 200:
            lines.append("…（后面的行省略）")
            break
    return "\n".join(lines)


def _shared_strings(archive: zipfile.ZipFile, names: list[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    import xml.etree.ElementTree as ET

    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    values = []
    for item in root.findall(_SPREADSHEET_NS + "si"):
        chunks = []
        for child in item:
            if child.tag == _SPREADSHEET_NS + "t":
                chunks.append(child.text or "")
            elif child.tag == _SPREADSHEET_NS + "r":
                node = child.find(_SPREADSHEET_NS + "t")
                if node is not None and node.text:
                    chunks.append(node.text)
        values.append("".join(chunks))
    return values


def _cell_value(cell, shared: list[str]) -> str:
    kind = cell.attrib.get("t", "")
    if kind == "inlineStr":
        node = cell.find(".//" + _SPREADSHEET_NS + "t")
        return node.text or "" if node is not None else ""
    node = cell.find(_SPREADSHEET_NS + "v")
    raw = node.text or "" if node is not None else ""
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    return raw


def _reject_zip(data: bytes, broken: str = "这份 Word 读不出来") -> None:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(broken) from exc
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ValueError(broken)
    total = sum(info.file_size for info in infos)
    if total > MAX_UNCOMPRESSED:
        raise ValueError(broken)
