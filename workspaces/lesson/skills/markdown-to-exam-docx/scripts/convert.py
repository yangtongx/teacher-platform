#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""试卷 Markdown → 试卷 Word（学生版 + 教师答案版）。

契约来源：`workspaces/lesson/AGENTS.md` 阶段3

    python3 skills/markdown-to-exam-docx/scripts/convert.py \
      --input tmp/exam.md --output exams/{试卷名}_试卷.docx \
      --answer-input tmp/answer.md --answer-output exams/{试卷名}_答案.docx

可选排版参数：`--font-cjk 楷体` / `--body-size 12` / `--line-spacing 1.2`

排版规范（契约原文）：大题号宋体加粗，正文宋体 + Times New Roman，五号字
（10.5pt），1.5 倍行距，选项缩进对齐。

学生版与答案版分别输出到两个文件；只给 `--input/--output` 时也能独立工作。
只用 python-docx + 标准库，不引入任何新依赖。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "错误：缺少依赖 python-docx。请先安装（pip install python-docx），再运行本脚本。\n"
    )
    raise SystemExit(2)

# ---------------------------------------------------------------- 排版常量

FONT_CJK = "宋体"
FONT_LATIN = "Times New Roman"
SIZE_BODY = 10.5          # 五号字
SIZE_TITLE = 16.0         # 试卷名称
SIZE_SECTION = 12.0       # 大题号
LINE_SPACING = 1.5

PAGE_W, PAGE_H = Cm(21.0), Cm(29.7)   # A4
MARGIN_TB = Cm(2.54)
MARGIN_LR = Cm(2.5)                   # 左右 2.5cm，给装订/答题留白
OPTION_INDENT = Cm(1.2)               # 选项缩进对齐

TITLE_RE = re.compile(r"^(.+?)\s*[（(]([^（()）]*?[）)])\s*$")
INFO_RE = re.compile(r"(满分|总分|时间|时长|分值)\s*[：:]\s*([0-9]+(?:\.[0-9]+)?)")
QUESTION_RE = re.compile(
    r"^\s*(\d{1,3})\s*[.、．)）]\s*[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]\s*(.*)$"
)
OPTION_RE = re.compile(r"^\s*([A-H])\s*[.、．)）]\s*(.*)$")
OPTION_MARK_RE = re.compile(r"(?:(?<=^)|(?<=\s))([A-H])\s*[.、．)）]\s*")
SCORE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*分")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
TABLE_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")
STARS_RE = re.compile(r"\*\*(.+?)\*\*")

# 答案版标记；学生版出现这些即视为答案泄漏
ANSWER_MARKERS = ("【答案】", "【难度】", "【解析】", "【评分细则】")
MARKER_RE = re.compile(r"【(答案|难度|解析|评分细则|知识点)】")
LABEL_RE = re.compile(r"(答案|难度|解析|评分细则|知识点)\s*[：:]")


def describe_markers(markers) -> str:
    return "、".join(f"【{name}】" for name in sorted(markers))
MARKER_REPLACE = {"答案": "【答案】", "难度": "【难度】",
                  "解析": "【解析】", "评分细则": "【评分细则】",
                  "知识点": "【知识点】"}


def configure_std_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


# ---------------------------------------------------------------- 字体工具


def set_run_font(run, cjk: str = FONT_CJK, latin: str = FONT_LATIN) -> None:
    """同时设置西文字体与 `w:eastAsia`，否则中文退化成默认字体。"""
    run.font.name = latin
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:ascii"), latin)
    rfonts.set(qn("w:hAnsi"), latin)
    rfonts.set(qn("w:eastAsia"), cjk)
    rfonts.set(qn("w:cs"), latin)


def add_runs(paragraph, text: str, *, cjk: str, latin: str, size: float,
             bold: bool = False) -> None:
    pos = 0
    for match in STARS_RE.finditer(text):
        if match.start() > pos:
            _plain_run(paragraph, text[pos:match.start()], cjk, latin, size, bold)
        _plain_run(paragraph, match.group(1), cjk, latin, size, True)
        pos = match.end()
    if pos < len(text):
        _plain_run(paragraph, text[pos:], cjk, latin, size, bold)


def _plain_run(paragraph, text: str, cjk: str, latin: str, size: float,
               bold: bool) -> None:
    if text == "":
        return
    run = paragraph.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    set_run_font(run, cjk, latin)


def style_paragraph(paragraph, *, line_spacing: float, space_after: float = 0.0,
                    space_before: float = 0.0, align=None, indent=None,
                    left_indent=None) -> None:
    fmt = paragraph.paragraph_format
    fmt.line_spacing = line_spacing
    fmt.space_after = Pt(space_after)
    fmt.space_before = Pt(space_before)
    if align is not None:
        paragraph.alignment = align
    if indent is not None:
        fmt.first_line_indent = indent
    if left_indent is not None:
        fmt.left_indent = left_indent


def setup_document(*, cjk: str, latin: str, body_size: float,
                   line_spacing: float, page_number: bool) -> "Document":
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = latin
    style.font.size = Pt(body_size)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:ascii"), latin)
    rfonts.set(qn("w:hAnsi"), latin)
    rfonts.set(qn("w:eastAsia"), cjk)
    rfonts.set(qn("w:cs"), latin)
    for section in doc.sections:
        section.page_width = PAGE_W
        section.page_height = PAGE_H
        section.top_margin = MARGIN_TB
        section.bottom_margin = MARGIN_TB
        section.left_margin = MARGIN_LR
        section.right_margin = MARGIN_LR
        if page_number:
            _add_page_number_footer(section, cjk=cjk, latin=latin, size=body_size)
    return doc


def _add_page_number_footer(section, *, cjk: str, latin: str, size: float) -> None:
    """页脚居中「第 X 页 共 Y 页」——试卷必须能对页，缺页学生看不出来。"""
    footer = section.footer
    footer.is_linked_to_previous = False
    para = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    para.text = ""
    style_paragraph(para, line_spacing=1.0, align=WD_ALIGN_PARAGRAPH.CENTER)
    prefix = para.add_run("第 ")
    page_field = OxmlElement("w:fldSimple")
    page_field.set(qn("w:instr"), "PAGE")
    para._p.append(page_field)
    middle = para.add_run(" 页 共 ")
    total_field = OxmlElement("w:fldSimple")
    total_field.set(qn("w:instr"), "NUMPAGES")
    para._p.append(total_field)
    suffix = para.add_run(" 页")
    for run in (prefix, middle, suffix):
        run.font.size = Pt(9)
        set_run_font(run, cjk, latin)


# ---------------------------------------------------------------- 解析


def split_table_row(line: str) -> list:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    cells, buf, i = [], "", 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == "|":
            buf += "|"
            i += 2
            continue
        if ch == "|":
            cells.append(buf.strip())
            buf = ""
            i += 1
            continue
        buf += ch
        i += 1
    cells.append(buf.strip())
    return cells


def is_table_separator(cells: list) -> bool:
    return bool(cells) and all(
        TABLE_SEP_CELL_RE.match(cell.replace(" ", "")) for cell in cells
    )


def parse_blocks(text: str) -> list:
    blocks = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        if line == "":
            i += 1
            continue
        heading = HEADING_RE.match(line)
        if heading:
            blocks.append({"type": "heading", "level": len(heading.group(1)),
                           "text": heading.group(2).strip()})
            i += 1
            continue
        if "|" in line and i + 1 < len(lines):
            head_cells = split_table_row(line)
            sep_cells = split_table_row(lines[i + 1])
            if len(head_cells) >= 2 and len(sep_cells) == len(head_cells) \
                    and is_table_separator(sep_cells):
                rows = [head_cells]
                i += 2
                while i < len(lines) and "|" in lines[i] and lines[i].strip():
                    cells = split_table_row(lines[i])
                    if len(cells) < len(head_cells):
                        cells += [""] * (len(head_cells) - len(cells))
                    rows.append(cells[:len(head_cells)])
                    i += 1
                blocks.append({"type": "table", "rows": rows})
                continue
        question = QUESTION_RE.match(line)
        if question:
            blocks.append({"type": "question", "number": question.group(1),
                           "score": float(question.group(2)),
                           "text": question.group(3).strip()})
            i += 1
            continue
        options = option_items(line)
        if options:
            for label, text in options:
                blocks.append({"type": "option", "label": label, "text": text})
            i += 1
            continue
        marker = MARKER_RE.search(line)
        if marker and line.startswith("【"):
            blocks.append({"type": "marker", "name": marker.group(1),
                           "text": MARKER_RE.sub("", line).strip()})
            i += 1
            continue
        if LABEL_RE.match(line):
            match = LABEL_RE.match(line)
            blocks.append({"type": "marker", "name": match.group(1),
                           "text": line[match.end():].strip()})
            i += 1
            continue
        blocks.append({"type": "para", "text": line})
        i += 1
    return blocks


def parse_exam_head(text: str) -> dict:
    """从 `# 试卷名称（满分100分 时间90分钟）` 里读出名称/总分/时长。"""
    info = {"name": "", "total": None, "duration": None, "extra": ""}
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line.startswith("# ") or line.startswith("##"):
            continue
        title = line[2:].strip()
        info["name"] = TITLE_RE.sub(r"\1", title) if TITLE_RE.match(title) else title
        for key, value in INFO_RE.findall(title):
            if key in ("满分", "总分", "分值") and info["total"] is None:
                info["total"] = float(value)
            elif key in ("时间", "时长") and info["duration"] is None:
                info["duration"] = float(value)
        break
    # 题干首行可能自带「满分：100分 时间：90分钟」信息行
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        for key, value in INFO_RE.findall(line):
            if key in ("满分", "总分", "分值") and info["total"] is None:
                info["total"] = float(value)
            elif key in ("时间", "时长") and info["duration"] is None:
                info["duration"] = float(value)
    return info


def detect_markers(text: str) -> set:
    found = set()
    for match in MARKER_RE.finditer(text):
        found.add(match.group(1))
    for match in LABEL_RE.finditer(text):
        found.add(match.group(1))
    return found


def strip_answer_markers(text: str) -> str:
    """删掉行内出现的答案/难度/解析标注（用于学生版兜底净化）。"""
    out = text
    while True:
        marker = MARKER_RE.search(out)
        label = LABEL_RE.search(out)
        cut = None
        if marker:
            cut = marker.start()
        if label and (cut is None or label.start() < cut):
            cut = label.start()
        if cut is None:
            return out.strip()
        out = out[:cut].rstrip(" \t；;，,。")


def is_answer_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("【"):
        return True
    return bool(LABEL_RE.match(stripped))


# ---------------------------------------------------------------- 渲染


def render_title(doc, head, *, cjk, latin, body_size, line_spacing) -> None:
    para = doc.add_paragraph()
    style_paragraph(para, line_spacing=line_spacing,
                    align=WD_ALIGN_PARAGRAPH.CENTER, space_after=6.0)
    add_runs(para, head["name"] or "试卷", cjk=cjk, latin=latin,
             size=SIZE_TITLE, bold=True)
    bits = []
    if head["total"] is not None:
        bits.append(f"满分：{head['total']:g} 分")
    if head["duration"] is not None:
        bits.append(f"时间：{head['duration']:g} 分钟")
    if bits:
        info = doc.add_paragraph()
        style_paragraph(info, line_spacing=line_spacing,
                        align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2.0)
        add_runs(info, "　".join(bits), cjk=cjk, latin=latin, size=body_size)


def group_options(blocks: list) -> list:
    """同一题连续的选项合成一行，Word 里横排。"""
    merged = []
    bucket = []

    def flush():
        if bucket:
            merged.append({"type": "options", "items": list(bucket)})
            bucket.clear()

    for block in blocks:
        if block["type"] == "option":
            bucket.append(block)
        else:
            flush()
            merged.append(block)
    flush()
    return merged


def option_items(line: str):
    """一行里的选择题选项。单独成行或「A. …　　B. …」都拆开。"""
    if not OPTION_RE.match(line):
        return None
    matches = list(OPTION_MARK_RE.finditer(line))
    if not matches:
        return None
    if line[:matches[0].start()].strip():
        return None
    # 少于三个选项标记时保持整行，避免把「A. 见 B.」拆开。
    if len(matches) < 3:
        return [(matches[0].group(1), line[matches[0].end():].strip())]
    items = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        items.append((match.group(1), line[match.end():end].strip()))
    return items


def render_blocks(doc, blocks, *, cjk, latin, body_size, line_spacing,
                  student: bool) -> None:
    blocks = group_options(blocks)
    for block in blocks:
        kind = block["type"]
        if kind == "heading":
            level = block["level"]
            if level <= 1:
                render_title(doc, {"name": block["text"], "total": None,
                                   "duration": None, "extra": ""},
                             cjk=cjk, latin=latin, body_size=body_size,
                             line_spacing=line_spacing)
            elif level == 2:
                para = doc.add_paragraph()
                style_paragraph(para, line_spacing=line_spacing,
                                space_before=8.0, space_after=4.0)
                add_runs(para, block["text"], cjk=cjk, latin=latin,
                         size=SIZE_SECTION, bold=True)
            else:
                para = doc.add_paragraph()
                style_paragraph(para, line_spacing=line_spacing,
                                space_before=4.0, space_after=2.0)
                add_runs(para, block["text"], cjk=cjk, latin=latin,
                         size=SIZE_SECTION, bold=True)
        elif kind == "question":
            text = block["text"]
            if student:
                text = strip_answer_markers(text)
            para = doc.add_paragraph()
            style_paragraph(para, line_spacing=line_spacing, space_after=2.0,
                            space_before=2.0)
            add_runs(para, f"{block['number']}.（{block['score']:g}分）{text}",
                     cjk=cjk, latin=latin, size=body_size)
        elif kind == "options":
            parts = []
            for item in block["items"]:
                text = item["text"]
                if student:
                    text = strip_answer_markers(text)
                parts.append(f"{item['label']}. {text}")
            para = doc.add_paragraph()
            style_paragraph(para, line_spacing=line_spacing, space_after=2.0,
                            left_indent=OPTION_INDENT)
            add_runs(para, "　　".join(parts), cjk=cjk, latin=latin,
                     size=body_size)
        elif kind == "marker":
            if student:
                continue
            para = doc.add_paragraph()
            style_paragraph(para, line_spacing=line_spacing, space_after=1.0,
                            left_indent=Cm(0.6))
            name = MARKER_REPLACE.get(block["name"], block["name"])
            add_runs(para, f"{name}{block['text']}", cjk=cjk, latin=latin,
                     size=body_size, bold=True)
        elif kind == "table":
            render_table(doc, block["rows"], cjk=cjk, latin=latin,
                         body_size=body_size)
        elif kind == "para":
            text = block["text"]
            if student:
                text = strip_answer_markers(text)
                if not text:
                    continue
            para = doc.add_paragraph()
            style_paragraph(para, line_spacing=line_spacing, space_after=2.0)
            add_runs(para, text, cjk=cjk, latin=latin, size=body_size)


def render_table(doc, rows, *, cjk, latin, body_size) -> None:
    cols = max(len(row) for row in rows)
    table = doc.add_table(rows=len(rows), cols=cols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for r_index, row in enumerate(rows):
        for c_index in range(cols):
            cell = table.cell(r_index, c_index)
            text = row[c_index] if c_index < len(row) else ""
            para = cell.paragraphs[0]
            style_paragraph(para, line_spacing=1.0, space_after=0.0,
                            align=WD_ALIGN_PARAGRAPH.CENTER if r_index == 0
                            else WD_ALIGN_PARAGRAPH.LEFT)
            add_runs(para, text, cjk=cjk, latin=latin, size=body_size,
                     bold=(r_index == 0))
    spacer = doc.add_paragraph()
    style_paragraph(spacer, line_spacing=1.0, space_after=0.0)


def build_document(text: str, *, cjk: str, latin: str, body_size: float,
                   line_spacing: float, student: bool,
                   page_number: bool) -> "Document":
    doc = setup_document(cjk=cjk, latin=latin, body_size=body_size,
                         line_spacing=line_spacing, page_number=page_number)
    blocks = parse_blocks(text)
    head = parse_exam_head(text)
    title_done = False
    body = []
    for block in blocks:
        if block["type"] == "heading" and block["level"] == 1:
            if not title_done:
                # `#` 试卷名称：居中放大，带上「满分/时间」信息行
                render_title(doc, head, cjk=cjk, latin=latin,
                             body_size=body_size, line_spacing=line_spacing)
                title_done = True
                continue
            block = {"type": "heading", "level": 2, "text": block["text"]}
        body.append(block)
    if not title_done:
        render_title(doc, head, cjk=cjk, latin=latin, body_size=body_size,
                     line_spacing=line_spacing)
    render_blocks(doc, body, cjk=cjk, latin=latin, body_size=body_size,
                  line_spacing=line_spacing, student=student)
    return doc


# ---------------------------------------------------------------- 质检


def collect_questions(blocks: list) -> list:
    """把 question 块与紧随其后的 option 块合并成题目记录。"""
    questions = []
    current = None
    for block in blocks:
        if block["type"] == "question":
            current = {"number": block["number"], "score": block["score"],
                       "text": block["text"], "options": []}
            questions.append(current)
            continue
        if block["type"] == "option" and current is not None:
            current["options"].append(block["label"])
    return questions


def check_exam(exam_text: str, answer_text: str) -> list:
    warnings = []
    if not exam_text.strip():
        warnings.append("学生版试卷为空")
    exam_blocks = parse_blocks(exam_text)
    answer_blocks = parse_blocks(answer_text) if answer_text else []

    exam_questions = collect_questions(exam_blocks)
    answer_questions = collect_questions(answer_blocks)
    if not exam_questions:
        warnings.append("学生版没有解析到任何题目，请检查题干是否为 `1.（3分）……` 格式")
    if answer_text and not answer_questions:
        warnings.append("答案版没有解析到任何题目，请检查是否包含完整题目")
    if answer_text and exam_questions and answer_questions \
            and len(exam_questions) != len(answer_questions):
        warnings.append(
            f"学生版题量（{len(exam_questions)}）与答案版题量"
            f"（{len(answer_questions)}）不一致"
        )

    for question in exam_questions:
        if len(question["options"]) not in (0, 4):
            warnings.append(
                f"第 {question['number']} 题的选项数为 {len(question['options'])}"
                "（选择题应为 4 个 A~D）"
            )

    head = parse_exam_head(exam_text)
    if head["total"] is not None and exam_questions:
        per_question = sum(q["score"] for q in exam_questions)
        declared = _declared_section_total(exam_blocks)
        if declared is not None and abs(declared - head["total"]) > 0.01:
            warnings.append(
                f"各大题标注分值之和（{declared:g}）与试卷总分"
                f"（{head['total']:g}）不一致"
            )
        elif abs(per_question - head["total"]) > 0.01 and declared is None:
            warnings.append(
                f"题干分值之和（{per_question:g}）与试卷总分"
                f"（{head['total']:g}）不一致"
            )
    return warnings


def _declared_section_total(blocks: list):
    total = 0.0
    for block in blocks:
        if block["type"] != "heading" or block["level"] != 2:
            continue
        scores = [float(v) for v in SCORE_RE.findall(block["text"])]
        if scores:
            total += scores[-1]
    return total if total > 0 else None


# ---------------------------------------------------------------- 主流程


def count_words(text: str) -> int:
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return cjk + words


def derive_answer_output(output: Path) -> Path:
    stem = output.stem
    if "试卷" in stem:
        return output.with_name(stem.replace("试卷", "答案") + output.suffix)
    return output.with_name(stem + "_答案" + output.suffix)


def write_docx(doc, out: Path) -> None:
    """统一保存并自动创建输出目录。"""
    if out.parent and str(out.parent) not in ("", "."):
        out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))


def main(argv=None) -> int:
    configure_std_streams()
    parser = argparse.ArgumentParser(
        prog="convert.py",
        description="把试卷 Markdown（tmp/exam.md）与答案 Markdown（tmp/answer.md）"
                    "转换为试卷版 / 答案版 Word 文档。",
        epilog="示例：python3 skills/markdown-to-exam-docx/scripts/convert.py "
               "--input tmp/exam.md --output exams/期中_试卷.docx "
               "--answer-input tmp/answer.md --answer-output exams/期中_答案.docx",
    )
    parser.add_argument("--input", required=True, help="学生版试卷 Markdown，如 tmp/exam.md")
    parser.add_argument("--output", required=True,
                        help="学生版 docx 输出路径，如 exams/{试卷名}_试卷.docx")
    parser.add_argument("--answer-input", default=None,
                        help="教师版答案 Markdown，如 tmp/answer.md（可选）")
    parser.add_argument("--answer-output", default=None,
                        help="答案版 docx 输出路径；给了 --answer-input 而没给本项时，"
                             "由 --output 推导（试卷→答案）")
    parser.add_argument("--font-cjk", default=FONT_CJK,
                        help=f"中文字体（默认 {FONT_CJK}，可换 楷体）")
    parser.add_argument("--font-latin", default=FONT_LATIN,
                        help=f"西文字体（默认 {FONT_LATIN}）")
    parser.add_argument("--body-size", type=float, default=SIZE_BODY,
                        help=f"正文字号，单位 pt（默认 {SIZE_BODY}，即五号）")
    parser.add_argument("--line-spacing", type=float, default=LINE_SPACING,
                        help=f"行距倍数（默认 {LINE_SPACING}）")
    parser.add_argument("--no-page-number", action="store_true",
                        help="不在页脚加「第 X 页 共 Y 页」")
    parser.add_argument("--no-warn", action="store_true",
                        help="不打印质检警告")
    args = parser.parse_args(argv)

    if args.body_size <= 0:
        sys.stderr.write(f"错误：--body-size 必须为正数，收到 {args.body_size}\n")
        return 2
    if args.line_spacing <= 0:
        sys.stderr.write(f"错误：--line-spacing 必须为正数，收到 {args.line_spacing}\n")
        return 2

    src = Path(args.input)
    if not src.is_file():
        sys.stderr.write(f"错误：输入文件不存在：{src}\n")
        return 2
    try:
        exam_text = src.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        sys.stderr.write(f"错误：输入文件不是 UTF-8 编码，无法解析：{src}（{exc}）\n")
        return 2
    if not exam_text.strip():
        sys.stderr.write(f"错误：输入文件为空：{src}。试卷 Markdown 不能是空文件。\n")
        return 2

    answer_text = ""
    if args.answer_input:
        answer_src = Path(args.answer_input)
        if not answer_src.is_file():
            sys.stderr.write(f"错误：答案输入文件不存在：{answer_src}\n")
            return 2
        try:
            answer_text = answer_src.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            sys.stderr.write(
                f"错误：答案文件不是 UTF-8 编码，无法解析：{answer_src}（{exc}）\n"
            )
            return 2
        if not answer_text.strip():
            sys.stderr.write(f"错误：答案文件为空：{answer_src}\n")
            return 2
    elif args.answer_output:
        sys.stderr.write("错误：给了 --answer-output 但没有 --answer-input，无法生成答案版\n")
        return 2

    # 学生版里混入答案标记是硬错误：会把答案发给学生
    leaked = detect_markers(exam_text)
    if leaked:
        sys.stderr.write(
            "错误：学生版试卷（--input）里出现了答案标记 "
            f"{describe_markers(leaked)}；"
            "试卷版不能包含答案/难度/解析，请把答案移到 --answer-input 指向的文件。\n"
        )
        return 3

    warnings = check_exam(exam_text, answer_text)
    if warnings and not args.no_warn:
        for item in warnings:
            sys.stderr.write(f"警告：{item}\n")

    style = dict(cjk=args.font_cjk, latin=args.font_latin,
                 body_size=args.body_size, line_spacing=args.line_spacing,
                 page_number=not args.no_page_number)
    try:
        exam_doc = build_document(exam_text, student=True, **style)
        exam_out = Path(args.output)
        write_docx(exam_doc, exam_out)

        lines = [f"试卷版：{exam_out.resolve()}（相对路径：{exam_out}）",
                 f"  学生版字数统计：{count_words(exam_text)} 字"]

        if answer_text:
            answer_out = Path(args.answer_output) if args.answer_output \
                else derive_answer_output(exam_out)
            answer_doc = build_document(answer_text, student=False, **style)
            write_docx(answer_doc, answer_out)
            lines.append(f"答案版：{answer_out.resolve()}（相对路径：{answer_out}）")
            lines.append(f"  答案版字数统计：{count_words(answer_text)} 字")
    except OSError as exc:
        sys.stderr.write(f"错误：写入 docx 失败：{exc}\n")
        return 1

    print(str(exam_out.resolve()))
    for line in lines:
        print(line)
    print(f"质检警告：{len(warnings)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
