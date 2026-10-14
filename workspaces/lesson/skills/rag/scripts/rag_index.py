#!/usr/bin/env python3
"""RAG 建索引技能：把 parse_doc.py 产出的 txt 切块写进 SQLite。

契约（来自 workspaces/lesson/AGENTS.md，不可更改）：
    python3 skills/rag/scripts/rag_index.py --input tmp/{x}.txt \
        --db rag/textbook.db --source-file uploads/{x}.pdf [--smart]

    --smart 语义：
        原始文件名不在 db 中        → 追加（新教材）
        原始文件名在 db 中且 MD5 相同 → 跳过（内容没变）
        原始文件名在 db 中但 MD5 不同 → 该文件全量重建（内容变了）

表结构（契约固定，禁止出现 documents 表）：
    chunks          切块正文
    vec_chunks      向量表。只有配置了 embedding 服务才填充，否则保持空表
    indexed_files   源文件登记（source_file 唯一 / md5 / size / chunk_count / indexed_at）
    fts_chunks      FTS5 辅助索引（--smart 跳过判定依赖它，中文用 bigram 分词）

中文检索为什么必须用 FTS5 + bigram：本机没有 embedding 服务（DeepSeek 不提供
embedding 接口），FTS5 自带分词器又把整句中文当成一个 token，所以索引和查询
两侧都必须走同一个 bigram 分词函数。

退出码：0 成功 / 2 输入不合法 / 4 写库失败。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# ---- 契约固定的三张表 ----
SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file   TEXT    NOT NULL,
    heading       TEXT    DEFAULT '',
    content       TEXT    NOT NULL,
    chunk_index   INTEGER NOT NULL,
    token_count   INTEGER DEFAULT 0,
    char_count    INTEGER DEFAULT 0,
    page_start    INTEGER DEFAULT 0,
    page_end      INTEGER DEFAULT 0,
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_file);

CREATE TABLE IF NOT EXISTS vec_chunks (
    chunk_pk    INTEGER PRIMARY KEY,
    source_file TEXT,
    model       TEXT,
    dim         INTEGER,
    vector      TEXT,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS indexed_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file  TEXT NOT NULL UNIQUE,
    md5          TEXT NOT NULL,
    size         INTEGER NOT NULL,
    chunk_count  INTEGER NOT NULL,
    indexed_at   TEXT NOT NULL,
    input_file   TEXT DEFAULT ''
);
"""

# FTS5 辅助索引：external content 只存索引，正文回 chunks 取，避免正文两份
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
    content,
    heading,
    content='chunks',
    content_rowid='chunk_id',
    tokenize='unicode61'
);
"""

DEFAULT_CHUNK_SIZE = 700
DEFAULT_OVERLAP = 80
MIN_CHUNK_SIZE = 100
MAX_CHUNK_SIZE = 4000

# 页分隔标记，与 skills/doc-parser/scripts/parse_doc.py 保持一致
PAGE_MARKER_RE = re.compile(r"^={3,}\s*第\s*(\d+)\s*页\s*={3,}$")
# 单行小节标题：第1章 / 第一章 / 第一节 / 一、 / Unit 3 / Lesson 2 / 3.2 ...
HEADING_RE = re.compile(
    r"^(?:"
    r"第\s*[0-9一二三四五六七八九十百]+\s*[章节课单元讲部分]"
    r"|[0-9]+\.[0-9]+(?:\.[0-9]+)*"
    r"|[一二三四五六七八九十]+\s*、"
    r"|(?:Unit|Lesson|Section|Part|Module|Chapter)\s+[0-9A-Za-z]+"
    r"|【[^】]{1,20}】"
    r")"
)
HEADING_MAX_LEN = 40
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# 断句标点（含闭引号/闭括号），切长段时优先在这些位置断开
SENTENCE_END = "。！？；!?;…"
CLOSERS = "”’」』）)】》"

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
EMBEDDING_TIMEOUT = 30
CHAT_TIMEOUT = 60

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_FAILED = 4


class SchemaError(Exception):
    """数据库结构不对（例如库里根本没有 chunks 表）。"""


class EmbeddingUnavailable(Exception):
    """embedding 服务不可用——此时绝不写入伪造向量。"""


# --------------------------------------------------------------------------
# 中文分词：FTS5 官方分词器对中文无效，这里自己做 bigram
# --------------------------------------------------------------------------
def tokenize(text: str) -> list[str]:
    """把文本切成检索 token：CJK 连续串切重叠 2 字，ASCII 单词转小写。

    索引与查询必须使用同一个函数，否则中文检索命中不了。
    """
    tokens: list[str] = []
    buffer: list[str] = []

    def flush_cjk() -> None:
        # 中文按 bigram：'分数的意义' → ['分数', '数的', '的意义'...]
        # 单字串保留单字，保证一个字的查询也能命中
        if not buffer:
            return
        if len(buffer) == 1:
            tokens.append(buffer[0])
        else:
            tokens.extend(buffer[i] + buffer[i + 1] for i in range(len(buffer) - 1))
        buffer.clear()

    for char in text:
        if CJK_RE.match(char):
            buffer.append(char)
            continue
        flush_cjk()
        if char.isascii() and (char.isalnum() or char == "_"):
            if tokens and tokens[-1].isascii() and tokens[-1].isalnum():
                tokens[-1] += char.lower()
            else:
                tokens.append(char.lower())
        elif char.isspace():
            tokens.append("\n")
        else:
            tokens.append(char)
    flush_cjk()
    return tokens


def index_tokens(text: str) -> list[str]:
    """入库用的 token 流：只保留有区分度的 token，去掉标点与换行。

    中文单字（例如标题「分数」被 bigram 切出的「分」「数」）也要保留，
    否则短标题块的 token_count 会是 0，FTS 索引里也会丢掉该标题。
    """
    return [
        tok
        for tok in tokenize(text)
        if tok != "\n" and (len(tok) > 1 or bool(CJK_RE.match(tok)))
    ]


def escape_fts_token(token: str) -> str:
    """把 token 包成 FTS5 的双引号短语，双引号本身翻倍转义。"""
    return '"' + token.replace('"', '""') + '"'


def build_fts_match(query: str) -> str:
    """查询串 → FTS5 MATCH 表达式（每个 token 作为独立短语）。"""
    tokens = [tok for tok in tokenize(query) if tok != "\n"]
    # 查询里已经带双引号（例如用户搜 "分数"）时原样透传，避免破坏 FTS5 语法
    if query.count('"') % 2 == 1:
        return query
    if not tokens:
        return ""
    return " OR ".join(escape_fts_token(tok) for tok in tokens)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_source_name(source: str) -> str:
    """源文件登记名统一用 POSIX 斜杠，Linux/Windows 上结果一致。"""
    return str(source).replace("\\", "/")


def extract_page_number(line: str) -> int | None:
    match = PAGE_MARKER_RE.match(line.strip())
    return int(match.group(1)) if match else None


def is_heading_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > HEADING_MAX_LEN:
        return False
    if extract_page_number(stripped) is not None:
        return True
    if HEADING_RE.match(stripped):
        return True
    # 短行且结尾没有句末标点 → 当标题（教材里的小标题大多如此）
    return not stripped.endswith(tuple(SENTENCE_END + "，,：:"))


# --------------------------------------------------------------------------
# 切块
# --------------------------------------------------------------------------
def _split_long_paragraph(paragraph: str, chunk_size: int) -> list[str]:
    """把超长段落按断句标点切成 <= chunk_size 的片段。"""
    pieces: list[str] = []
    buffer = ""
    index = 0
    length = len(paragraph)

    while index < length:
        char = paragraph[index]
        buffer += char
        if char in SENTENCE_END and len(buffer) >= chunk_size // 2:
            pieces.append(buffer.strip())
            buffer = ""
        elif len(buffer) >= chunk_size:
            pieces.append(buffer.strip())
            buffer = ""
        index += 1

    if buffer.strip():
        pieces.append(buffer.strip())
    return [piece for piece in pieces if piece]


def _add_chunk(
    chunks: list[dict],
    text: str,
    heading: str,
    page_no: int | None,
    position: int,
) -> None:
    chunks.append(
        {
            "content": text.strip(),
            "heading": heading.strip(),
            "position": position,
            "page": page_no,
        }
    )


def _chunk_page(paragraphs: list[str], page_no: int | None, chunk_size: int, overlap: int) -> list[dict]:
    """把一页内的段落合并成 500~800 字级别的块，块间保留轻微重叠。"""
    chunks: list[dict] = []
    current = ""
    position = 0
    current_heading = ""

    for raw in paragraphs:
        paragraph = raw.strip()
        if not paragraph:
            continue

        if is_heading_line(paragraph):
            # 标题行自成一块（可以和后面的正文合并，但不和前面的正文合并）
            if current:
                _add_chunk(chunks, current, current_heading, page_no, position)
                position += 1
                current = ""
            current_heading = paragraph

        if len(paragraph) > chunk_size:
            for piece in _split_long_paragraph(paragraph, chunk_size):
                if current and len(current) + len(piece) > chunk_size:
                    _add_chunk(chunks, current, current_heading, page_no, position)
                    position += 1
                    current = current[-overlap:] if overlap > 0 else ""
                current += ("\n" if current else "") + piece
            continue

        if current and len(current) + len(paragraph) + 1 > chunk_size:
            _add_chunk(chunks, current, current_heading, page_no, position)
            position += 1
            current = current[-overlap:] if overlap > 0 else ""

        current += ("\n" if current else "") + paragraph

    if current.strip():
        _add_chunk(chunks, current, current_heading, page_no, position)

    return chunks


def iter_chunks(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    source_file: str = "",
) -> list[dict]:
    """把整篇正文切成块。返回 [{content, heading, position, page, tokens, char_count}]。"""
    if chunk_size < MIN_CHUNK_SIZE:
        raise ValueError(f"--chunk-size 太小：{chunk_size}，最小 {MIN_CHUNK_SIZE}")
    if chunk_size > MAX_CHUNK_SIZE:
        raise ValueError(f"--chunk-size 太大：{chunk_size}，最大 {MAX_CHUNK_SIZE}")
    if overlap < 0:
        raise ValueError(f"--overlap 不能为负数：{overlap}")
    if overlap >= chunk_size:
        raise ValueError(f"--overlap({overlap}) 必须小于 --chunk-size({chunk_size})")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")

    # 按页切段，逐页切块（块不跨页，便于溯源页码）
    segments: list[tuple[int | None, list[str]]] = []
    current_page: int | None = None
    current_paragraphs: list[str] = []
    for line in normalized.split("\n"):
        page_no = extract_page_number(line)
        if page_no is not None:
            segments.append((current_page, current_paragraphs))
            current_page = page_no
            current_paragraphs = []
            continue
        if line.strip():
            current_paragraphs.append(line)
    segments.append((current_page, current_paragraphs))

    raw_chunks: list[dict] = []
    for page_no, paragraphs in segments:
        raw_chunks.extend(_chunk_page(paragraphs, page_no, chunk_size, overlap))

    result: list[dict] = []
    for index, chunk in enumerate(raw_chunks):
        content = chunk["content"]
        # 索引文本带上标题（如果有），提升命中率；原始 content 保持干净
        indexed = f"{chunk['heading']}\n{content}" if chunk["heading"] else content
        result.append(
            {
                "content": content,
                "heading": chunk["heading"],
                "position": index,
                "page_start": chunk["page"] or 0,
                "page_end": chunk["page"] or 0,
                "tokens": index_tokens(indexed),
                "char_count": len(content),
                "source_file": source_file,
            }
        )
    return result


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------
def connect(db_path: Path) -> sqlite3.Connection:
    """打开（必要时创建）数据库并建好表结构。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    try:
        conn.executescript(FTS_SCHEMA)
    except sqlite3.Error as exc:  # FTS5 未编译进来时明确报错，不静默降级
        raise SchemaError(
            f"建 FTS5 索引失败（本机 sqlite3 可能不支持 FTS5）：{exc}"
        ) from exc
    conn.commit()


def clear_file(conn: sqlite3.Connection, source_file: str) -> int:
    """删掉某个源文件的全部切块（含 FTS 辅助索引），返回删除条数。"""
    rows = conn.execute(
        "SELECT chunk_id FROM chunks WHERE source_file = ?", (source_file,)
    ).fetchall()
    deleted = len(rows)
    if deleted and table_exists(conn, "fts_chunks"):
        # external content 表不会跟随基表删除，必须显式清理，否则会留下幽灵命中
        conn.executemany(
            "INSERT INTO fts_chunks(fts_chunks, rowid, content, heading) "
            "VALUES('delete', ?, '', '')",
            [(row["chunk_id"],) for row in rows],
        )
    conn.execute("DELETE FROM chunks WHERE source_file = ?", (source_file,))
    conn.execute("DELETE FROM vec_chunks WHERE source_file = ?", (source_file,))
    return deleted


def get_indexed_file(conn: sqlite3.Connection, source_file: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM indexed_files WHERE source_file = ?", (source_file,)
    ).fetchone()
    return dict(row) if row else None


def upsert_indexed_file(
    conn: sqlite3.Connection,
    source_file: str,
    md5: str,
    size: int,
    chunk_count: int,
    input_file: str,
) -> None:
    conn.execute(
        "INSERT INTO indexed_files(source_file, md5, size, chunk_count, indexed_at, input_file) "
        "VALUES(?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source_file) DO UPDATE SET "
        "md5 = excluded.md5, size = excluded.size, chunk_count = excluded.chunk_count, "
        "indexed_at = excluded.indexed_at, input_file = excluded.input_file",
        (source_file, md5, size, chunk_count, now_iso(), str(input_file)),
    )


def insert_chunks(conn: sqlite3.Connection, source_file: str, chunks: list[dict]) -> list[int]:
    """写入 chunks + fts_chunks，返回落库后的 chunk_id 列表（顺序与 chunks 一致）。"""
    created = now_iso()
    chunk_ids: list[int] = []
    has_fts = table_exists(conn, "fts_chunks")

    for chunk in chunks:
        cursor = conn.execute(
            "INSERT INTO chunks(source_file, heading, content, chunk_index, token_count, "
            "char_count, page_start, page_end, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_file,
                chunk["heading"],
                chunk["content"],
                chunk["position"],
                len(chunk["tokens"]),
                chunk["char_count"],
                chunk["page_start"],
                chunk["page_end"],
                created,
            ),
        )
        chunk_id = int(cursor.lastrowid)
        chunk_ids.append(chunk_id)
        if has_fts:
            conn.execute(
                "INSERT INTO fts_chunks(rowid, content, heading) VALUES(?, ?, ?)",
                (chunk_id, " ".join(chunk["tokens"]), " ".join(index_tokens(chunk["heading"]))),
            )
    return chunk_ids


def delete_stale_files(conn: sqlite3.Connection, days: int) -> list[str]:
    """删除 indexed_at 早于 days 天前的源文件登记（用于淘汰旧教材）。"""
    cutoff = (_dt.datetime.now().astimezone() - _dt.timedelta(days=days)).isoformat(
        timespec="seconds"
    )
    rows = conn.execute(
        "SELECT source_file FROM indexed_files WHERE indexed_at < ?", (cutoff,)
    ).fetchall()
    removed = [row["source_file"] for row in rows]
    for source_file in removed:
        clear_file(conn, source_file)
        conn.execute("DELETE FROM indexed_files WHERE source_file = ?", (source_file,))
    return removed


# --------------------------------------------------------------------------
# embedding（本机没有该服务时为 no-op，绝不伪造向量）
# --------------------------------------------------------------------------
def embedding_config() -> dict | None:
    """读取 embedding 配置。三个环境变量齐全才算启用，否则返回 None。"""
    base_url = (os.environ.get("EMBEDDING_BASE_URL") or "").strip()
    api_key = (os.environ.get("EMBEDDING_API_KEY") or "").strip()
    model = (os.environ.get("EMBEDDING_MODEL") or "").strip()
    if not (base_url and api_key and model):
        return None
    return {"base_url": base_url.rstrip("/"), "api_key": api_key, "model": model}


def embeddings_enabled() -> bool:
    return embedding_config() is not None


def _http_embed(texts: list[str], config: dict) -> list[list[float]]:
    """真正发 HTTP 的 embedding 调用（单独函数，方便测试 monkeypatch）。"""
    import requests  # 延迟导入：没有 embedding 服务时完全不碰网络库

    response = requests.post(
        f"{config['base_url']}/embeddings",
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        },
        json={"model": config["model"], "input": texts},
        timeout=EMBEDDING_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    data = sorted(payload.get("data", []), key=lambda item: item.get("index", 0))
    return [item["embedding"] for item in data]


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """有 embedding 服务就返回向量，没有就返回 None（调用方不要写任何东西）。"""
    config = embedding_config()
    if config is None:
        raise EmbeddingUnavailable(
            "embedding 服务未配置：缺少 EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL。"
            "未配置时 vec_chunks 保持空表，绝不写入伪造向量；去掉 --embed 即可只用 BM25。"
        )
    vectors = _http_embed(texts, config)
    if len(vectors) != len(texts):
        raise EmbeddingUnavailable(
            f"embedding 服务返回条数不匹配：期望 {len(texts)}，实际 {len(vectors)}"
        )
    return vectors


def store_vectors(conn: sqlite3.Connection, source_file: str, chunk_ids: list[int], vectors) -> None:
    """把向量写进 vec_chunks（JSON 文本存储，避免依赖 numpy/数组库）。"""
    config = embedding_config() or {}
    created = now_iso()
    for chunk_id, vector in zip(chunk_ids, vectors):
        conn.execute(
            "INSERT OR REPLACE INTO vec_chunks(chunk_pk, source_file, model, dim, vector, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (
                chunk_id,
                source_file,
                config.get("model", ""),
                len(vector),
                json.dumps([float(value) for value in vector]),
                created,
            ),
        )


# --------------------------------------------------------------------------
# 主逻辑（可被 import 调用）
# --------------------------------------------------------------------------
def plan_action(
    smart: bool,
    rebuild: bool,
    existing: dict | None,
    md5: str,
) -> str:
    """决定这次索引的动作：index / append / skip / rebuild。

    对应 AGENTS.md「智能模式判断逻辑」：
        existing is None            → index（追加，新教材）
        existing.md5 == md5         → skip（跳过，内容没变）
        existing.md5 != md5         → rebuild（全量重建，内容变了）
    --rebuild 显式强制重建；不带 --smart 时同一源文件重复索引按 rebuild 处理（幂等）。
    测试直接断言这个函数。
    """
    if rebuild:
        return "index" if existing is None else "rebuild"
    if existing is None:
        return "index"
    if not smart:
        return "rebuild"
    if existing["md5"] == md5:
        return "skip"
    return "rebuild"


def index_file(
    input_path: Path | str,
    db_path: Path | str,
    source_file: str,
    *,
    smart: bool = False,
    rebuild: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    embed: bool = False,
) -> dict:
    """把单个 txt 建/更新进索引库。幂等：同一份内容重复运行结果稳定。"""
    input_path = Path(input_path)
    db_path = Path(db_path)
    source_file = normalize_source_name(source_file)

    if not input_path.is_file():
        raise ValueError(f"输入 txt 不存在：{input_path}")

    text = input_path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"输入 txt 是空的：{input_path}")

    md5 = file_md5(input_path)
    size = input_path.stat().st_size

    vectors = None
    if embed:
        # 探活：服务没配或不可用时立刻抛错，避免写出半套数据
        embed_texts([""])

    conn = connect(db_path)
    try:
        existing = get_indexed_file(conn, source_file)
        action = plan_action(smart, rebuild, existing, md5)
        chunks = iter_chunks(text, chunk_size=chunk_size, overlap=overlap, source_file=source_file)

        if action == "skip":
            conn.commit()
            return {
                "action": "skip",
                "source_file": source_file,
                "md5": md5,
                "size": size,
                "chunk_count": existing["chunk_count"],
                "db": str(db_path),
                "input_file": str(input_path),
                "warning": None,
            }

        clear_file(conn, source_file)
        chunk_ids = insert_chunks(conn, source_file, chunks)

        warning = None
        if embed:
            texts = [chunk["content"] for chunk in chunks]
            try:
                vectors = embed_texts(texts)
            except Exception as exc:  # 任何网络/服务异常都不影响 BM25 索引
                vectors = None
                warning = f"embedding 调用失败，vec_chunks 保持空表（不影响 BM25 检索）：{exc}"
            if vectors is not None:
                store_vectors(conn, source_file, chunk_ids, vectors)

        upsert_indexed_file(conn, source_file, md5, size, len(chunks), str(input_path))
        conn.commit()
    finally:
        conn.close()

    return {
        "action": action,
        "source_file": source_file,
        "md5": md5,
        "size": size,
        "chunk_count": len(chunks),
        "db": str(db_path),
        "input_file": str(input_path),
        "warning": warning,
    }


ACTION_TEXT = {
    "index": "追加（新文件全量建）",
    "append": "追加",
    "skip": "跳过",
    "rebuild": "全量重建",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rag_index.py",
        description="把教材/教参 txt 切块写入 SQLite 索引库（中文用 FTS5 BM25 检索）。",
        epilog=(
            "示例：python3 skills/rag/scripts/rag_index.py --input tmp/教材.txt "
            "--db rag/textbook.db --source-file uploads/教材.pdf --smart"
        ),
    )
    parser.add_argument("--input", required=True, help="parse_doc.py 产出的 txt 路径")
    parser.add_argument("--db", required=True, help="目标数据库，例如 rag/textbook.db")
    parser.add_argument(
        "--source-file",
        required=True,
        help="源文件登记名，例如 uploads/教材.pdf（indexed_files 的唯一键）",
    )
    parser.add_argument(
        "--smart",
        action="store_true",
        help="智能模式：新文件追加 / MD5 相同跳过 / MD5 变化全量重建",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="强制删除该文件的旧切块后全量重建",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"切块目标长度（字符），默认 {DEFAULT_CHUNK_SIZE}，范围 {MIN_CHUNK_SIZE}-{MAX_CHUNK_SIZE}",
    )
    parser.add_argument(
        "--overlap",
        type=int,
        default=DEFAULT_OVERLAP,
        help=f"块间重叠字符数，默认 {DEFAULT_OVERLAP}",
    )
    parser.add_argument(
        "--embed",
        action="store_true",
        help="额外生成向量写入 vec_chunks（需要 EMBEDDING_BASE_URL/API_KEY/MODEL）",
    )
    parser.add_argument(
        "--max-idle-days",
        type=int,
        default=None,
        help="可选：删除超过 N 天未更新的旧源文件登记（默认不删）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    db_path = Path(args.db)
    source_file = normalize_source_name(args.source_file)

    if not input_path.is_file():
        print(f"错误：输入 txt 不存在：{input_path}", file=sys.stderr)
        return EXIT_BAD_INPUT
    if not input_path.read_text(encoding="utf-8").strip():
        print(f"错误：输入 txt 是空的：{input_path}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        result = index_file(
            input_path,
            db_path,
            source_file,
            smart=args.smart,
            rebuild=args.rebuild,
            chunk_size=args.chunk_size,
            overlap=args.overlap,
            embed=args.embed,
        )
    except (ValueError, SchemaError, EmbeddingUnavailable) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except (OSError, sqlite3.Error) as exc:
        print(f"错误：写库失败：{exc}", file=sys.stderr)
        return EXIT_FAILED

    if args.max_idle_days is not None:
        conn = connect(db_path)
        try:
            removed = delete_stale_files(conn, args.max_idle_days)
            conn.commit()
        finally:
            conn.close()
        if removed:
            print(f"清理超过 {args.max_idle_days} 天未更新的源文件：{len(removed)} 个")
            for name in removed:
                print(f"  - {name}")

    print(f"[{ACTION_TEXT.get(result['action'], result['action'])}] {result['source_file']}")
    print(f"切块数：{result['chunk_count']}（chunk-size={args.chunk_size}, overlap={args.overlap}）")
    print(f"数据库：{result['db']}")
    if result.get("warning"):
        print(f"警告：{result['warning']}", file=sys.stderr)

    if embeddings_enabled():
        print(f"向量检索：已启用（EMBEDDING_MODEL={embedding_config()['model']}）")
    else:
        print(
            "向量检索未启用，当前使用 BM25："
            "vec_chunks 保持空表。如需向量检索，请配置 "
            "EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL 并加 --embed。"
        )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
