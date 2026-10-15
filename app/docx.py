"""把一条 Markdown 回复收成 Word，给当前登录的老师下载。"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

FONT_CJK = "宋体"
FONT_LATIN = "Times New Roman"
HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
UNORDERED_RE = re.compile(r"^\s*[-*]\s+(.*)$")
ORDERED_RE = re.compile(r"^\s*\d+[.、)]\s+(.*)$")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
CODE_RE = re.compile(r"`([^`]+)`")
UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\r\n]+')


def document_name(markdown: str) -> str:
    for line in markdown.splitlines():
        text = line.strip().lstrip("#").strip()
        text = BOLD_RE.sub(r"\1", text)
        text = UNSAFE_NAME.sub("", text).strip()
        if text:
            return text[:40]
    return "文稿"


def _set_run_font(run, *, size: float, bold: bool = False) -> None:
    run.bold = bold
    run.font.size = Pt(size)
    run.font.name = FONT_LATIN
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_CJK)


def _add_runs(paragraph, text: str, *, size: float, bold: bool = False) -> None:
    pieces = CODE_RE.split(BOLD_RE.sub("\x00\\1\x00", text))
    # After splitting code, bold markers were turned into \x00...\x00 inside non-code pieces.
    raw_parts = []
    for index, piece in enumerate(pieces):
        if index % 2 == 1:
            raw_parts.append(("code", piece))
        else:
            raw_parts.append(("text", piece))
    for kind, piece in raw_parts:
        if kind == "code":
            run = paragraph.add_run(piece)
            _set_run_font(run, size=size)
            run.font.name = "Consolas"
            continue
        bits = piece.split("\x00")
        for bit_index, bit in enumerate(bits):
            if not bit:
                continue
            run = paragraph.add_run(bit)
            _set_run_font(run, size=size, bold=bold or bit_index % 2 == 1)


def write_docx(markdown: str, path: Path) -> None:
    document = Document()
    section = document.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2.5)
    style = document.styles["Normal"]
    style.font.name = FONT_LATIN
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), FONT_CJK)

    for raw in markdown.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            document.add_paragraph("")
            continue
        heading = HEADING_RE.match(line.strip())
        if heading:
            level = len(heading.group(1))
            paragraph = document.add_heading(level=level)
            _add_runs(paragraph, heading.group(2).strip(), size={1: 16, 2: 14, 3: 12}[level], bold=True)
            continue
        unordered = UNORDERED_RE.match(line)
        ordered = ORDERED_RE.match(line)
        if unordered or ordered:
            paragraph = document.add_paragraph(style="List Bullet" if unordered else "List Number")
            _add_runs(paragraph, (unordered or ordered).group(1).strip(), size=10.5)
            continue
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.line_spacing = 1.5
        _add_runs(paragraph, line.strip(), size=10.5)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)


def save_material(user, conversation_id: int, title: str, markdown: str) -> dict:
    """把一份文稿收成 Word，挂到当前对话，给老师下载。"""
    import secrets

    from app import db
    from app.config import DATA_DIR

    owned = db.conversation_owner(conversation_id)
    if owned is None or int(owned["user_id"]) != int(user["id"]):
        raise ValueError("这份对话不能导出文稿。")
    body = (markdown or "").strip()
    heading = " ".join(str(title or "").split())[:40]
    if not body:
        raise ValueError("文稿是空的，没有导出。")
    name = heading or document_name(body)
    if not body.lstrip().startswith("#"):
        body = f"# {name}\n\n{body}"
    message_id = db.latest_message_id(conversation_id)
    if message_id is None:
        raise ValueError("找不到这次对话，文稿没有保存。")
    token = secrets.token_urlsafe(16)
    filename = f"{document_name('# ' + name)}.docx"
    write_docx(body, DATA_DIR / "docs" / f"{token}.docx")
    db.save_document(int(user["id"]), message_id, token, filename)
    return {"name": filename, "url": f"/files/{token}"}
