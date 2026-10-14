#!/usr/bin/env python3
"""RAG 检索技能：在 SQLite(FTS5) 索引库里做中文检索，可选 DeepSeek 重排。

契约（来自 workspaces/lesson/AGENTS.md，不可更改）：
    python3 skills/rag/scripts/rag_query.py --db rag/textbook.db \
        --query "{80~150字的假设性答案}" --top-k 5 --rerank

检索分两段：
  1) 必选：FTS5 BM25。中文没有 embedding 服务可用，FTS5 原生分词器对中文无效，
     所以索引和查询都走 rag_index.tokenize() 的 bigram 分词。
  2) 可选：--rerank 用 DeepSeek 对话接口给候选打分重排。没有 DEEPSEEK_API_KEY
     或调用失败时降级为纯 BM25 结果，只在 stderr 说明，不改变退出码。

退出码：0 成功 / 2 输入或数据库不合法 / 4 检索失败。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# 允许 `python3 skills/rag/scripts/rag_query.py` 直接运行时 import 同目录模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rag_index import (  # noqa: E402  (必须在 sys.path 调整之后导入)
    CJK_RE,
    CHAT_TIMEOUT,
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
    SchemaError,
    build_fts_match,
    embeddings_enabled,
    tokenize,
)

DEFAULT_TOP_K = 5
DEFAULT_MAX_CHARS = 1200
# 重排候选池上限：多了既慢又容易触发模型的注意力衰减
RERANK_CANDIDATE_CAP = 10
# BM25 的绝对值随语料和查询长度漂移（小语料里 IDF 甚至会让相关片段的 BM25 正好等于 0），
# 没法直接和模型的 0~10 分加权。所以先把同一批候选用 BM25 归一化到 [0, 1]，再叠加模型分：
#     最终分 = BM25 归一化(0~1) + 1.5 × 模型分(0~10)
# 语料本身词面匹配很干净时 BM25 说了算；模型分数差大于 1 分（10 分制里很小的一档）
# 就能改变顺序——这正是 --rerank 的用途。代价是模型给满分时确实能压过 BM25 第 1 名。
RERANK_WEIGHT = 1.5
NORM_FLOOR = 0.0
NORM_CEIL = 1.0


class DbError(Exception):
    """数据库不存在或结构不对。"""


class RerankError(Exception):
    """重排不可用（缺 key、网络失败、返回格式不对）。"""


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------
def open_index(db_path: Path) -> sqlite3.Connection:
    """只读打开已有的索引库；库或表缺失时抛 DbError（不偷偷建库）。"""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise DbError(
            f"数据库不存在：{db_path}。请先用 rag_index.py 建索引"
            "（例如：python3 skills/rag/scripts/rag_index.py --input tmp/教材.txt "
            "--db rag/textbook.db --source-file uploads/教材.pdf）"
        )
    if not os.access(db_path, os.R_OK):
        raise DbError(f"数据库不可读（检查文件权限）：{db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    missing = [
        name
        for name in ("chunks", "vec_chunks", "indexed_files")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)
        ).fetchone()
        is None
    ]
    if missing:
        conn.close()
        raise DbError(
            f"数据库表结构不完整，缺少：{'、'.join(missing)}。"
            "契约表结构为 chunks / vec_chunks / indexed_files（没有 documents 表），"
            "请重新用 rag_index.py 建库"
        )
    return conn


def _row_to_hit(row: sqlite3.Row, bm25: float, score: float) -> dict:
    """把一行数据库记录转成对外结果（正文的截断在 query_index 里统一做）。"""
    text = row["content"] or ""
    hit = {
        "chunk_id": row["chunk_id"],
        "source_file": row["source_file"],
        "heading": row["heading"] or "",
        "chunk_index": row["chunk_index"],
        "page_start": row["page_start"],
        "page_end": row["page_end"],
        "score": round(score, 4),
        "bm25": round(bm25, 4),
        "norm": 0.0,
        "text": text,
    }
    return hit


def normalize_scores(hits: list[dict], field: str = "bm25") -> list[float]:
    """把同一批候选的分数归一化到 [0, 1]（越大越相关）。

    用同一批候选的内部相对值，而不是 BM25 绝对值：语料规模一变绝对值就漂，
    小语料里 IDF 甚至会让相关片段的 BM25 正好等于 0，直接展示会被误读成"不相关"。
    所有候选分数相同时（典型的极小语料）退化为按名次给分：第 1 名 1.0，其余 0.0，
    名次顺序仍然稳定。
    """
    count = len(hits)
    if count <= 0:
        return []
    if count == 1:
        return [NORM_CEIL]

    values = [float(hit.get(field, 0.0)) for hit in hits]
    low, high = min(values), max(values)
    span = NORM_CEIL - NORM_FLOOR
    if high - low <= 1e-12:
        return [NORM_CEIL] + [NORM_FLOOR] * (count - 1)
    return [NORM_FLOOR + span * (value - low) / (high - low) for value in values]


def bm25_search(
    db_path: Path | str,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    source_prefix: str | None = None,
) -> list[dict]:
    """FTS5 BM25 检索。返回按相关度降序的命中列表。

    hit["bm25"] 是原始 BM25 分（取负，越大越相关，仅作诊断）；
    hit["norm"]/hit["score"] 是按名次归一化的相对分，用于 --rerank 加权和排序稳定性。
    语料极小时 IDF 可能为 0，原始 bm25 会正好是 0.0——命中与否看的是有没有返回记录。

    `source_prefix`（2026-09-16 追加）：只检索 `source_file` 以它开头的片段。
    多学科拆 agent 后每个学科一个库，但**同一学科有多个年级**，年级就靠
    `uploads/<年级>/` 这个目录前缀区分 —— 所以检索时带上年级前缀，避免
    "问高二却召回高一"的内容。前缀为空表示不过滤（保持旧行为）。
    """
    match = build_fts_match(query)
    if not match:
        return []

    sql = (
        "SELECT c.chunk_id, c.source_file, c.heading, c.content, c.chunk_index, "
        "       c.page_start, c.page_end, bm25(fts_chunks) AS bm25_score "
        "FROM fts_chunks JOIN chunks c ON c.chunk_id = fts_chunks.rowid "
        "WHERE fts_chunks MATCH ? "
    )
    params: list[object] = [match]
    if source_prefix:
        sql += "AND c.source_file LIKE ? "
        params.append(f"{source_prefix}%")
    sql += "ORDER BY bm25_score ASC, c.source_file ASC, c.chunk_index ASC LIMIT ?"
    params.append(max(top_k, 1))

    conn = open_index(Path(db_path))
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()

    hits = []
    for row in rows:
        # SQLite 的 bm25() 越小越相关，取负让"分数越大越相关"
        bm25 = -float(row["bm25_score"])
        hits.append(_row_to_hit(row, bm25, bm25))

    # 归一化到 [0.5, 1]：既作为展示分数，也作为 --rerank 的加权基准
    for hit, norm in zip(hits, normalize_scores(hits)):
        hit["norm"] = round(norm, 4)
        hit["score"] = round(norm, 4)
    return hits


def _query_char_tokens(query: str) -> list[str]:
    """查询里长度 >= 2 的 CJK 片段（bigram 之外再做一次整词包含判断）。"""
    tokens = [tok for tok in tokenize(query) if len(tok) >= 2 and CJK_RE.search(tok)]
    return tokens


def substring_search(
    db_path: Path | str,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    source_prefix: str | None = None,
) -> list[dict]:
    """兜底检索：FTS5 一个 token 都没命中时，按字面片段覆盖率排序。

    这是纯字面匹配，不是向量相似度——命中不了就是命中不了，不编造分数。
    同样支持 `source_prefix` 前缀过滤（语义见 bm25_search）。
    """
    tokens = _query_char_tokens(query)
    if not tokens:
        return []

    conn = open_index(Path(db_path))
    try:
        sql = (
            "SELECT chunk_id, source_file, heading, content, chunk_index, "
            "       page_start, page_end FROM chunks"
        )
        params: tuple[object, ...] = ()
        if source_prefix:
            sql += " WHERE source_file LIKE ?"
            params = (f"{source_prefix}%",)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    scored = []
    for row in rows:
        text = row["content"] or ""
        haystack = f"{row['heading'] or ''}\n{text}"
        matched = sum(1 for token in tokens if token in haystack)
        if matched:
            scored.append((matched / len(tokens), row))
    scored.sort(key=lambda item: (-item[0], item[1]["source_file"], item[1]["chunk_index"]))

    hits = []
    for ratio, row in scored[: max(top_k, 1)]:
        hits.append(_row_to_hit(row, ratio, ratio))

    for hit, norm in zip(hits, normalize_scores(hits, field="bm25")):
        hit["norm"] = round(norm, 4)
        hit["score"] = round(norm, 4)
    return hits


def search(
    db_path: Path | str,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    source_prefix: str | None = None,
) -> list[dict]:
    """检索入口：先 BM25，无命中再字面兜底。`source_prefix` 见 bm25_search。"""
    hits = bm25_search(db_path, query, top_k, source_prefix)
    if hits:
        return hits
    return substring_search(db_path, query, top_k, source_prefix)


def list_sources(db_path: Path | str) -> list[dict]:
    """列出库里登记过的源文件与各自块数（按路径排序）。

    用途（2026-09-16 追加）：助手在出题前先看清"这个库里到底有哪些年级的材料"，
    而不是凭猜测直接检索、命中不了就说"没有"。多学科拆 agent 后每个学科一个库，
    年级就是 `uploads/<年级>/…` 的目录前缀，这份清单就是年级视角的目录。
    """
    conn = open_index(Path(db_path))
    try:
        rows = conn.execute(
            "SELECT source_file, COUNT(*) AS chunks FROM chunks "
            "GROUP BY source_file ORDER BY source_file ASC"
        ).fetchall()
    finally:
        conn.close()
    return [{"source_file": row["source_file"], "chunks": int(row["chunks"])} for row in rows]


# --------------------------------------------------------------------------
# 重排（DeepSeek 对话接口，网络调用单独封装，便于测试 monkeypatch）
# --------------------------------------------------------------------------
def deepseek_config() -> dict | None:
    """读取 DeepSeek 对话接口配置，缺 key 返回 None。"""
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        return None
    return {
        "api_key": api_key,
        "base_url": (os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL).strip().rstrip("/"),
        "model": (os.environ.get("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL).strip(),
        "timeout": CHAT_TIMEOUT,
    }


def deepseek_rerank_enabled() -> bool:
    return deepseek_config() is not None


def _http_chat(messages: list[dict], config: dict) -> str:
    """真正发 HTTP 的对话调用（单独函数，测试里 monkeypatch 它即可离线）。"""
    import requests  # 延迟导入：离线场景完全不碰网络库

    response = requests.post(
        f"{config['base_url']}/chat/completions",
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "model": config["model"],
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=config.get("timeout", CHAT_TIMEOUT),
    )
    response.raise_for_status()
    payload = response.json()
    return payload["choices"][0]["message"]["content"]


def _build_rerank_messages(query: str, hits: list[dict]) -> list[dict]:
    candidates = [
        {"id": hit["chunk_id"], "text": hit["text"][:600]} for hit in hits
    ]
    system = (
        "你是教材检索结果重排器。给定老师的问题和若干候选片段，"
        "给每个片段打 0~10 分表示它对回答该问题的帮助程度（10 最有用，0 完全无关）。"
        '只输出 JSON，格式为 {"scores": {"<id>": <分数>}}，不要解释、不要多余字段。'
    )
    user = (
        f"问题：\n{query}\n\n候选片段（JSON）：\n"
        f"{json.dumps(candidates, ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_rerank_scores(raw: str) -> dict[int, float]:
    """把模型返回解析成 {chunk_id: 分数}；解析不了就抛 RerankError。"""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RerankError(f"重排返回不是合法 JSON：{raw[:200]!r}") from exc

    scores = payload.get("scores") if isinstance(payload, dict) else None
    if not isinstance(scores, dict):
        raise RerankError(f"重排返回缺少 scores 字段：{raw[:200]!r}")

    parsed: dict[int, float] = {}
    for key, value in scores.items():
        try:
            parsed[int(key)] = float(value)
        except (TypeError, ValueError):
            continue
    if not parsed:
        raise RerankError(f"重排返回没有任何可用分数：{raw[:200]!r}")
    return parsed


def rerank(query: str, hits: list[dict]) -> dict[int, float]:
    """调用 DeepSeek 给候选打分。任何异常都抛 RerankError，由调用方降级。"""
    config = deepseek_config()
    if config is None:
        raise RerankError("未配置 DEEPSEEK_API_KEY")
    if not hits:
        return {}
    try:
        raw = _http_chat(_build_rerank_messages(query, hits), config)
    except Exception as exc:
        raise RerankError(f"调用 DeepSeek 失败：{type(exc).__name__}: {exc}") from exc
    return parse_rerank_scores(raw)


# --------------------------------------------------------------------------
# 主逻辑
# --------------------------------------------------------------------------
def query_index(
    db_path: Path | str,
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    rerank_enabled: bool = False,
    max_chars: int = DEFAULT_MAX_CHARS,
    candidate_cap: int = RERANK_CANDIDATE_CAP,
    source_prefix: str | None = None,
) -> dict:
    """检索主逻辑。返回 {hits, reranked, warnings, engine, vector_enabled}。"""
    if top_k < 1:
        raise ValueError(f"--top-k 必须 >= 1，当前 {top_k}")
    if not query.strip():
        raise ValueError("--query 不能为空")

    warnings: list[str] = []
    pool_size = max(top_k, min(candidate_cap, top_k * 3)) if rerank_enabled else top_k
    hits = search(db_path, query, pool_size, source_prefix)
    reranked = False

    if rerank_enabled:
        if not deepseek_rerank_enabled():
            warnings.append(
                "未设置 DEEPSEEK_API_KEY，--rerank 已降级为纯 BM25 结果（顺序不变）"
            )
        elif hits:
            try:
                scores = rerank(query, hits)
            except RerankError as exc:
                warnings.append(f"重排失败，已降级为纯 BM25 结果：{exc}")
            else:
                for hit in hits:
                    model_score = scores.get(hit["chunk_id"])
                    hit["rerank_score"] = round(model_score, 4) if model_score is not None else None
                    if model_score is not None:
                        # 归一化相关度(0~1) 为主，模型分(0~10) 按 RERANK_WEIGHT 加权
                        hit["score"] = round(hit["norm"] + RERANK_WEIGHT * model_score, 4)
                hits.sort(key=lambda hit: (-hit["score"], hit["source_file"], hit["chunk_index"]))
                reranked = True

    # 补上序号和截断后的正文
    final = []
    for index, hit in enumerate(hits[:top_k], start=1):
        item = dict(hit)
        item["rank"] = index
        item["text"] = item["text"][:max_chars]
        final.append(item)

    return {
        "query": query,
        "source_prefix": source_prefix or "",
        "engine": "bm25+deepseek-rerank" if reranked else "bm25",
        "reranked": reranked,
        "vector_enabled": embeddings_enabled(),
        "vector_note": (
            "向量检索已启用（EMBEDDING_* 已配置）"
            if embeddings_enabled()
            else "向量检索未启用，当前使用 BM25（FTS5 + 中文 bigram 分词）；"
            "配置 EMBEDDING_BASE_URL/EMBEDDING_API_KEY/EMBEDDING_MODEL 并给 "
            "rag_index.py 加 --embed 可启用"
        ),
        "warnings": warnings,
        "hits": final,
    }


def format_human(result: dict) -> str:
    """给人看的输出格式。"""
    lines = [
        f"查询：{result['query']}",
        f"检索方式：{result['engine']}",
    ]
    if result.get("source_prefix"):
        lines.append(f"范围过滤：只检索 {result['source_prefix']} 开头的材料")
    for warning in result["warnings"]:
        lines.append(f"警告：{warning}")
    lines.append("")

    if not result["hits"]:
        if result.get("source_prefix"):
            lines.append(
                f"没有命中任何片段（已限定范围 {result['source_prefix']}）。"
                f"可先跑 --list-sources 看这个库/这个范围里到底有哪些材料。"
            )
        else:
            lines.append("没有命中任何片段。")
        return "\n".join(lines)

    for hit in result["hits"]:
        location = hit["source_file"]
        if hit["heading"]:
            location += f" / {hit['heading']}"
        if hit["page_start"]:
            location += f"（第 {hit['page_start']} 页）"
        score = f"相关度 {hit['score']:.4f}"
        if hit.get("rerank_score") is not None:
            score += (
                f"（BM25 归一 {hit['norm']:.4f} + {RERANK_WEIGHT:g} × 模型分 "
                f"{hit['rerank_score']:g}）"
            )
        lines.append(f"[{hit['rank']}] {location}")
        lines.append(f"    {score}")
        for text_line in hit["text"].splitlines():
            lines.append(f"    {text_line}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rag_query.py",
        description="在 RAG 索引库里做中文检索（FTS5 BM25，可选 DeepSeek 重排）。",
        epilog=(
            "示例：python3 skills/rag/scripts/rag_query.py --db rag/textbook.db "
            '--query "分数表示把单位1平均分后取其中的几份" --top-k 5 --rerank'
        ),
    )
    parser.add_argument("--db", required=True, help="索引库路径，例如 rag/textbook.db")
    parser.add_argument(
        "--query",
        required=False,
        default=None,
        help="检索语句。契约要求用 80~150 字的假设性答案（HyDE），不要用短关键词。"
        "（--list-sources 时可不填）",
    )
    parser.add_argument(
        "--source-prefix",
        default=None,
        help="只检索 source_file 以该前缀开头的片段，例如 uploads/高二/ —— 多学科拆 agent 后"
        "用它按年级圈定范围（同一学科多年级时必填，避免问到高二却召回高一）",
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="只列出库里已登记的材料及块数（年级视角的目录），不做检索；可与 --json 搭配",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"返回条数，默认 {DEFAULT_TOP_K}",
    )
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="用 DeepSeek 对话接口重排；无 key 或失败时降级为纯 BM25",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出，便于程序消费",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help=f"每条结果正文最多输出多少字符，默认 {DEFAULT_MAX_CHARS}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # --list-sources：只列材料清单（年级视角目录），不检索
    if args.list_sources:
        try:
            sources = list_sources(Path(args.db))
        except (ValueError, DbError, SchemaError) as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        except (OSError, sqlite3.Error) as exc:
            print(f"错误：读取失败：{exc}", file=sys.stderr)
            return 4
        if args.json:
            print(json.dumps({"db": args.db, "sources": sources}, ensure_ascii=False, indent=2))
        elif not sources:
            print(f"库里还没有任何材料：{args.db}（先 uploads/ 放文件，再用 rag_index.py 建索引）")
        else:
            print(f"库：{args.db}（共 {len(sources)} 份材料）")
            for item in sources:
                print(f"  {item['source_file']}  —— {item['chunks']} 块")
        return 0

    if not (args.query or "").strip():
        print("错误：--query 不能为空（除非用 --list-sources 只列清单）", file=sys.stderr)
        return 2

    try:
        result = query_index(
            Path(args.db),
            args.query,
            top_k=args.top_k,
            rerank_enabled=args.rerank,
            max_chars=args.max_chars,
            source_prefix=args.source_prefix,
        )
    except (ValueError, DbError, SchemaError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(f"错误：检索失败：{exc}", file=sys.stderr)
        return 4

    for warning in result["warnings"]:
        print(f"警告：{warning}", file=sys.stderr)
    # 未启用向量检索时明确说明当前走的是 BM25（启用后就不再误导）
    if not result["vector_enabled"]:
        print(
            "说明：向量检索未启用，当前使用 BM25（FTS5 + 中文 bigram 分词）。"
            "原因与启用方式见 skills/rag/SKILL.md",
            file=sys.stderr,
        )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_human(result), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
