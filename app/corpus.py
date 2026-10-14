from __future__ import annotations

import json
import re
from pathlib import Path

from app.config import CORPUS_ROOT
from app.rag import search


def scope_ids(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return []
    return list(dict.fromkeys(value))


STAGE_NAMES = {"primary": "小学", "junior": "初中", "senior": "高中"}
SUBJECT_NAMES = {
    "chinese": "语文",
    "math": "数学",
    "english": "英语",
    "morality": "道德与法治",
    "politics": "思想政治",
    "history": "历史",
    "geography": "地理",
    "physics": "物理",
    "chemistry": "化学",
    "biology": "生物",
}
VERSION_NAMES = {
    "tb": "统编版",
    "rj": "人教版",
    "bsd": "北师大版",
    "sj": "苏教版",
    "hk": "沪科版",
    "wy": "外研版",
    "yl": "译林版",
    "yuelu": "岳麓版",
    "xj": "湘教版",
    "zt": "中图版",
    "yuej": "粤教版",
    "lj": "鲁教版",
}


def _catalog_scopes(root: Path) -> list[dict]:
    path = root / "curriculum-catalog.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    found = []
    for scope in data.get("scopes") or []:
        if not isinstance(scope, dict) or not isinstance(scope.get("id"), str):
            continue
        parts = scope["id"].split("/")
        if len(parts) != 4:
            continue
        stage, subject, version, label = parts
        found.append(
            {
                "id": scope["id"],
                "stage": STAGE_NAMES.get(stage, stage),
                "subject": SUBJECT_NAMES.get(subject, subject),
                "version": VERSION_NAMES.get(version, version),
                "label": str(scope.get("label") or label),
            }
        )
    return found


def _catalog_ids(root: Path) -> dict[str, str]:
    return {scope["id"]: scope["label"] for scope in _catalog_scopes(root)}


def catalog_choices(root: Path | None = None) -> list[dict]:
    return _catalog_scopes(root or CORPUS_ROOT)


def known_scope_ids(root: Path | None = None) -> set[str]:
    return {scope["id"] for scope in catalog_choices(root)}


def _field(user: object, name: str):
    if isinstance(user, dict):
        return user.get(name)
    try:
        return user[name]  # type: ignore[index]
    except (KeyError, IndexError):
        return None


def account_scope_ids(user: object) -> list[str]:
    configured = scope_ids(_field(user, "assigned_scopes"))
    if configured is None:
        stage, subject = _field(user, "stage"), _field(user, "subject")
        version, grade = _field(user, "textbook_version"), _field(user, "grade")
        if not (stage and subject and version and grade):
            return []
        return [f"{stage}/{subject}/{version}/{grade}"]
    return configured


def textbook_dbs(user: object, root: Path | None = None) -> list[dict]:
    """这位老师被授权的教材索引。路径只从范围 ID 拼出来，不跟模型猜。"""
    corpus = root or CORPUS_ROOT
    labels = _catalog_ids(corpus)
    configured = account_scope_ids(user)
    if not configured:
        return []
    for scope_id in configured:
        labels.setdefault(scope_id, scope_id.rsplit("/", 1)[-1])
    books = []
    for scope_id in configured:
        parts = scope_id.split("/")
        if len(parts) != 4 or any(part in ("", ".", "..") for part in parts):
            continue
        path = corpus.joinpath(*parts, "rag", "textbook.db")
        if not path.is_file():
            continue
        books.append({"id": scope_id, "label": labels.get(scope_id, parts[-1]), "path": path})
    return books


def _indexes(books: list[dict]) -> list[dict]:
    found = []
    for book in books:
        found.append(book)
        guide = book["path"].with_name("teacher_guide.db")
        if guide.is_file():
            found.append({**book, "label": f"{book['label']}教参", "path": guide})
    return found


def _search_queries(query: str) -> list[str]:
    text = query.strip()
    found = [text]
    for piece in re.split(r"[。！？\n；;]", text):
        piece = piece.strip()
        if len(piece) >= 6 and piece not in found:
            found.append(piece)
        if len(found) >= 3:
            break
    return found


def lesson_excerpts(user: object, query: str, *, limit: int = 12) -> str:
    books = _indexes(textbook_dbs(user))
    if not books or not query.strip():
        return ""
    hits: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    queries = _search_queries(query)
    for book in books[:8]:
        for index, piece in enumerate(queries):
            top_k = 4 if index == 0 else 2
            for hit in search(book["path"], piece, top_k=top_k):
                key = (str(hit.get("source_file") or ""), str(hit.get("heading") or ""), str(hit.get("content") or "")[:120])
                if key in seen:
                    continue
                seen.add(key)
                hit["label"] = book["label"]
                hits.append(hit)
    hits.sort(key=lambda item: item["score"], reverse=True)
    lines = []
    used = 0
    for hit in hits[:limit]:
        text = " ".join(hit["content"].split())
        if len(text) > 700:
            text = text[:700] + "…"
        page = ""
        if hit.get("page_start"):
            page = f"，约第{hit['page_start']}页"
        source = Path(str(hit.get("source_file") or "")).name
        lines.append(f"- [{hit['label']}{page} {source}] {hit['heading']} {text}".strip())
        used += len(lines[-1])
        if used > 7000:
            break
    if not lines:
        return ""
    return "教材摘录（只根据这些原文，不要补充摘录里没有的课文）：\n" + "\n".join(lines)


def textbook_notice(user: object) -> str:
    books = textbook_dbs(user)
    if not books:
        return "这个账号还没有对上的教材，教案不能按课文检索。"
    labels = "、".join(book["label"] for book in books[:6])
    return f"教案会在这些教材里检索：{labels}。"
