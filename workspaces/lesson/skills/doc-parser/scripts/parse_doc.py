#!/usr/bin/env python3
"""教材/教参文档解析技能：把 PDF / Word 解析成可处理的纯文本。

契约（来自 workspaces/lesson/AGENTS.md，不可更改）：
    python3 skills/doc-parser/scripts/parse_doc.py uploads/{教材原文件名}.pdf
    → 输出到 tmp/{原文件名}.txt（同 basename，扩展名换成 .txt）

限制：单文件 100MB 以内、1000 页以内（PDF 按页数，Word 按内容块数）。

退出码：
    0 成功
    2 输入不合法（文件不存在 / 类型不支持 / 超过大小或页数上限 / 解析不出文字）
    3 缺少依赖（PyMuPDF / python-docx 未安装）
    4 解析或写盘失败

所有 open() 都显式指定 encoding="utf-8"。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---- 契约常量：AGENTS.md「注意事项」写死了 100MB / 1000 页 ----
MAX_FILE_BYTES = 100 * 1024 * 1024  # 100MB
MAX_PAGES = 1000

# 页间分隔标记（人可读、机器可切分；数字页号方便 RAG 记录来源页）
PAGE_SEPARATOR_TEMPLATE = "===== 第 {page} 页 ====="

SUPPORTED_SUFFIXES = {".pdf", ".docx"}

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_MISSING_DEP = 3
EXIT_FAILED = 4


class ParseError(Exception):
    """输入不合法：文件不存在、类型不支持、超限、解析不出文字。"""


class DepMissingError(Exception):
    """运行环境缺少第三方依赖。"""


# --------------------------------------------------------------------------
# 参数与前置校验
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parse_doc.py",
        description="把教材/教参 PDF 或 Word 解析成纯文本，供 RAG 建索引使用。",
        epilog=(
            "示例：python3 skills/doc-parser/scripts/parse_doc.py "
            "uploads/人教版数学七年级上册.pdf"
        ),
    )
    parser.add_argument(
        "input",
        help="待解析文件路径（契约位置参数），例如 uploads/教材.pdf",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="输出 txt 路径；默认 tmp/<原文件名>.txt（原 basename 换扩展名）",
    )
    parser.add_argument(
        "--max-size-mb",
        type=float,
        default=MAX_FILE_BYTES / (1024 * 1024),
        help="单文件大小上限（MB），默认 100，超过直接报错",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=MAX_PAGES,
        help="单文件页数上限，默认 1000，超过直接报错",
    )
    parser.add_argument(
        "--no-reflow",
        action="store_true",
        help=(
            # 注意：argparse 会对 help 做 % 格式化，正文里的百分号必须写成 %%
            "关闭 PDF 段落重排，保留原始视觉换行。默认**开启**重排：实测（真教材）"
            "能把索引体积降 58%%、并让检索命中的是完整段落而不是十几字的碎片。"
            "只在需要「一行一行」的原始形态时使用"
        ),
    )
    return parser


def default_output_path(input_path: Path) -> Path:
    """契约默认输出：tmp/<原 basename>.txt，相对当前工作目录（工作区根）。"""
    return Path("tmp") / (input_path.stem + ".txt")


def validate_input(input_path: Path, max_size_mb: float, max_pages: int) -> None:
    """启动时的轻量校验：存在性、类型、大小。"""
    if not input_path.exists():
        raise ParseError(f"输入文件不存在：{input_path}")
    if not input_path.is_file():
        raise ParseError(f"输入路径不是文件：{input_path}")

    suffix = input_path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = "、".join(sorted(SUPPORTED_SUFFIXES))
        raise ParseError(
            f"不支持的文件类型：{input_path.suffix or '（无扩展名）'}。"
            f"本脚本只支持 {supported}"
        )

    size = input_path.stat().st_size
    if size == 0:
        raise ParseError(f"输入文件是空的（0 字节）：{input_path}")
    limit = int(max_size_mb * 1024 * 1024)
    if size > limit:
        raise ParseError(
            f"文件超过大小上限：{size / 1024 / 1024:.1f}MB > {max_size_mb:g}MB。"
            "请拆分教材后再解析"
        )


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
def _import_fitz():
    # PyMuPDF 1.24+ 的正式模块名是 pymupdf，`fitz` 只是兼容别名，
    # 用别名会在 stderr 打出一条弃用告警 —— 而 agent 的 exec 工具会读 stderr，
    # 这条噪声会混进模型的上下文里，所以优先用正式名。
    try:
        import pymupdf  # type: ignore

        return pymupdf
    except ImportError:
        pass
    try:
        import fitz  # type: ignore
    except ImportError as exc:  # pragma: no cover - 服务器上已装 PyMuPDF
        raise DepMissingError(
            f"缺少 PyMuPDF（import pymupdf / fitz 均失败）：{exc}。"
            "请安装：pip install PyMuPDF"
        ) from exc
    return fitz


def reflow_paragraphs(text: str) -> str:
    """把 PDF 抽出来的"视觉换行"合并回段落。

    为什么必须做（2026-09-17 用真教材实测出来的）：PDF 的文字层是**按行**排的，
    `get_text("text")` 出来的就是一行一行。实测《义务教育教科书·语文八年级下册》
    （150 页）：抽出 104,357 字符，却有 **5,723 行、平均每行 16.7 字符**；
    直接拿去切块会变成"一行一块" —— 4,003 块、平均 24 字符/块，
    而 `chunk-size` 本该是 700。后果有两个，都实测到了：

      * **索引暴涨**：每块都有固定开销，块越小越亏。同一份正文，
        合并段落后索引从 2.23 MB 降到 0.93 MB（**-58%**），索引/正文 从 7.83 倍降到 3.32 倍；
      * **检索质量差**：命中的"教材原文"只有十几个字（实测 top1 是
        `j，便得一山，山有小口，仿佛` 这种碎片），老师没法拿去写教案。

    规则（刻意保守、可预测）：
      * 以 `。！？…"』】）` 等**句末标点**结尾的行 → 视为段落结束；
      * 否则与下一行**直接拼接**（中文排版里换行不是语义边界，不加空格）；
      * 空行 → 段落分隔；
      * `===== 第 N 页 =====` 这类页标记行 → 原样保留，且不参与拼接
        （页码是 RAG 的引用依据，绝不能被并进正文）。
    """
    out: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("===== 第 ") and line.endswith("====="):
            if buf:
                out.append(buf)
                buf = ""
            out.append(line)
            continue
        if not line:
            if buf:
                out.append(buf)
                buf = ""
            continue
        buf = (buf + line) if buf else line
        if ENDS_PARAGRAPH_RE.search(line):
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return "\n".join(out)


# 句末标点（含中文引号/括号收尾）——用于判断"这一行是不是一段的结尾"
ENDS_PARAGRAPH_RE = re.compile(r"[。！？…”』】」）\)]$")


def parse_pdf(input_path: Path, max_pages: int, reflow: bool = True) -> tuple[str, int, int]:
    """逐页抽取文本。返回 (合并后的正文, 页数, 没抽到文字的页数)。

    `reflow=True`（默认）会对**每一页**做段落重排 —— 逐页做是为了让页码归属保持精确
    （重排只影响页内换行，不跨页搬动文字）。

    【关于"没抽到文字的页"】返回这个数是为了**诚实地暴露扫描页**：有些教材/教参是
    "正文页有文字层、插图页或部分章节是扫描图"的混合体，只报总字符数看不出来。
    检索不会命中这些页上的内容，老师应当知道。

    【不要把这些字母当解析错误】PDF 抽出来的正文里常见**行首孤立字母**
    （`b。那时我的祖母…`、`d〔礼数〕礼节。`），它们是教材的**注释序号**（a、b、c…；
    《桃花源记》的注释能排到 j、m），是**真实内容**，不是字体映射错误。
    我最初把它误判成"文字层字符错映射"并加了警告，实测对照注释后才确认判错，已撤掉 ——
    一个会把正确内容报成错误的诊断，比没有诊断更糟（会让老师不敢相信正确的原文）。
    """
    fitz = _import_fitz()

    try:
        doc = fitz.open(str(input_path))
    except Exception as exc:  # fitz 抛的异常类型很杂，统一转成可读错误
        raise ParseError(f"无法打开 PDF（可能已损坏或加密）：{exc}") from exc

    try:
        page_count = doc.page_count
        if page_count == 0:
            raise ParseError(f"PDF 没有任何页面：{input_path}")
        if page_count > max_pages:
            raise ParseError(
                f"PDF 页数超过上限：{page_count} 页 > {max_pages} 页。"
                "请拆分教材后再解析"
            )

        pieces: list[str] = []
        raw_lines = 0
        empty_pages = 0
        for index in range(page_count):
            page = doc.load_page(index)
            text = page.get_text("text") or ""
            if not text.strip():
                empty_pages += 1
            raw_lines += len([l for l in text.splitlines() if l.strip()])
            body = reflow_paragraphs(text) if reflow else text.strip()
            pieces.append(f"{PAGE_SEPARATOR_TEMPLATE.format(page=index + 1)}\n{body}")
        if not raw_lines:
            raise ParseError(
                f"解析不出文字：{input_path}。"
                "如果是扫描版 PDF（图片型），本脚本不做 OCR，请提供带文字层的版本"
            )
    finally:
        doc.close()

    return "\n\n".join(pieces), page_count, empty_pages

# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------
def _import_docx():
    try:
        import docx  # type: ignore
    except ImportError as exc:  # pragma: no cover - 服务器上已装 python-docx
        raise DepMissingError(
            f"缺少 python-docx（import docx 失败）：{exc}。请安装：pip install python-docx"
        ) from exc
    return docx


def table_to_lines(table) -> list[str]:
    """把 Word 表格转成可读文本行：单元格用 ' | ' 分隔，行首加 [表格]。"""
    lines: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        for cell in row.cells:
            # 单元格里可能有多个段落，用顿号连起来
            parts = [p.text.strip() for p in cell.paragraphs if p.text.strip()]
            cells.append("、".join(parts))
        if any(cells):
            lines.append("[表格] " + " | ".join(cells))
    return lines


def _iter_docx_blocks(document):
    """按正文顺序遍历段落与表格，避免表格单元格里的段落被重复输出。

    python-docx >= 1.1 提供 iter_inner_content()；旧版本退化为只走段落。
    """
    if hasattr(document, "iter_inner_content"):
        yield from document.iter_inner_content()
        return

    from docx.table import Table  # pragma: no cover - 兼容旧版本
    from docx.text.paragraph import Paragraph  # pragma: no cover - 兼容旧版本

    body = document.element.body  # pragma: no cover
    for child in body.iterchildren():  # pragma: no cover
        if child.tag.endswith("}p"):
            yield Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield Table(child, document)


def parse_docx(input_path: Path, max_pages: int) -> tuple[str, int]:
    """抽取段落与表格。返回 (正文, 内容块数)。"""
    docx = _import_docx()

    try:
        document = docx.Document(str(input_path))
    except Exception as exc:
        raise ParseError(f"无法打开 DOCX（可能已损坏或不是 Word 文件）：{exc}") from exc

    from docx.table import Table

    blocks: list[str] = []
    for block in _iter_docx_blocks(document):
        if isinstance(block, Table):
            blocks.extend(table_to_lines(block))
        else:
            text = block.text.strip()
            if text:
                blocks.append(text)

    if len(blocks) > max_pages:
        raise ParseError(
            f"DOCX 内容块数超过上限：{len(blocks)} 块 > {max_pages} 块。"
            "（Word 没有真实页数，这里用段落+表格行数代替页数上限）"
        )

    return "\n\n".join(blocks), len(blocks)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def parse(input_path: Path, max_size_mb: float, max_pages: int,
          reflow: bool = True) -> tuple[str, int, str, int]:
    """解析入口。返回 (正文, 单元数(页/块), 单元名, 无文字页数)。"""
    validate_input(input_path, max_size_mb, max_pages)

    suffix = input_path.suffix.lower()
    empty_pages = 0
    if suffix == ".pdf":
        text, units, empty_pages = parse_pdf(input_path, max_pages, reflow=reflow)
        unit_name = "页"
    else:
        text, units = parse_docx(input_path, max_pages)
        unit_name = "块"

    if not text.strip():
        raise ParseError(
            f"没有解析出任何文字：{input_path}。"
            "如果是扫描版 PDF（图片型），本脚本不做 OCR，请提供带文字层的版本"
        )
    return text, units, unit_name, empty_pages


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else default_output_path(input_path)

    try:
        text, units, unit_name, empty_pages = parse(input_path, args.max_size_mb, args.max_pages,
                                                    reflow=not args.no_reflow)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        # 统一 LF 写盘，Windows 上也不产生 CRLF，方便后续切分
        with open(output_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
    except ParseError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except DepMissingError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_MISSING_DEP
    except OSError as exc:
        print(f"错误：写盘失败：{exc}", file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:  # 兜底：任何解析异常都必须非零退出，不能假装成功
        print(f"错误：解析失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    print(f"解析{unit_name}数：{units}")
    print(f"字符数：{len(text)}")
    if input_path.suffix.lower() == ".pdf":
        print(f"段落重排：{'已启用（PDF 的视觉换行已合并成段落）' if not args.no_reflow else '已关闭（--no-reflow）'}")
    print(f"输出路径：{output_path}")
    if empty_pages:
        # 诚实地暴露"混合体"：正文页有文字层、部分页是扫描图。检索不会命中这些页上的内容。
        print(
            f"注意：有 {empty_pages} 页没有抽到文字（共 {units} 页）。"
            "这些页多半是插图/扫描图，本脚本不做 OCR —— 检索不会命中其中的内容；"
            "若关键章节正好落在这些页上，需要另找带文字层的版本。",
            file=sys.stderr,
        )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
