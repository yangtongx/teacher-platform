"""在教材库里按中文 bigram 检索。查询和建索引必须用同一套切词，否则对不上。"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
MAX_QUERY_TOKENS = 40


def tokenize(text: str) -> list[str]:
    """与教材索引同一套 bigram。查询和入库必须用同一套，否则中文对不上。"""
    tokens: list[str] = []
    buffer: list[str] = []

    def flush_cjk() -> None:
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


def build_fts_match(query: str) -> str:
    tokens = [tok for tok in tokenize(query) if tok != "\n"][:MAX_QUERY_TOKENS]
    if not tokens:
        return ""
    return " OR ".join('"' + tok.replace('"', '""') + '"' for tok in tokens)


def search(db_path: Path, query: str, top_k: int = 4) -> list[dict]:
    match = build_fts_match(query)
    if not match or not db_path.is_file():
        return []
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT c.heading, c.content, c.page_start, c.page_end, c.source_file,
                   bm25(fts_chunks) AS bm25_score
            FROM fts_chunks JOIN chunks c ON c.chunk_id = fts_chunks.rowid
            WHERE fts_chunks MATCH ?
            ORDER BY bm25_score ASC
            LIMIT ?
            """,
            (match, max(top_k, 1)),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    hits = []
    for row in rows:
        hits.append(
            {
                "heading": row["heading"] or "",
                "content": row["content"] or "",
                "page_start": row["page_start"],
                "page_end": row["page_end"],
                "source_file": row["source_file"] or "",
                "score": -float(row["bm25_score"]),
            }
        )
    return hits
