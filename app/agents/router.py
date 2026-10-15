"""主助手只做分拣，不出题、不写通知。

清楚的请求按关键词交给对应身份。看不出来时再问一次快速模型。
仍看不出来，就只追问一句。一句话里两件事时，先做先出现的那件。
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from app.config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, IDENTITIES
from app.exam import OFFICE_RE, is_exam_request

LOG = logging.getLogger("teacher-platform")

ASK_QUESTION = "这件是出题、写教案，还是写通知、纪要、家长沟通？"

LESSON_RE = re.compile(
    r"教案|课件|组卷|出题|命题|试卷|试题|习题|练习题|选择题|填空题|课时|单元测|参考答案|出一套|出一道|出几道|分层作业|说课|教学反思"
)
OFFICE_WORD_RE = re.compile(r"通知|纪要|家长信|致家长|请柬|润色|发言稿|公示|家长会|家长沟通|会议记录|给家长|家长消息")
STRONG_LESSON_RE = re.compile(r"试卷|试题|出题|教案|课件|组卷|命题|分层|说课|教学反思")
FOLLOW_RE = re.compile(r"^(再|继续|接着|沿用|还是|改|换一|这个|那个|上面|刚才|同样|也|好的|可以|行|嗯|对|不用|不需要)")
# 一句话里用标点拆开，前半句和后半句可以分给不同专家。
SPLIT_RE = re.compile(r"[，。；！？\n]+")


def _kind(text: str) -> str:
    lesson = bool(LESSON_RE.search(text) or is_exam_request(text))
    office = bool(OFFICE_WORD_RE.search(text) or OFFICE_RE.search(text))
    if lesson and office:
        if STRONG_LESSON_RE.search(text):
            return "lesson"
        return "office"
    if lesson:
        return "lesson"
    if office:
        return "office"
    return ""


def _followup(text: str, previous: str | None) -> bool:
    if previous not in IDENTITIES:
        return False
    if FOLLOW_RE.search(text):
        return True
    return len(text) <= 8


def decide(content: str, previous: str | None) -> dict:
    """不访问模型。route 为 lesson、office 或 ask。"""
    text = (content or "").strip()
    parts = [part.strip() for part in SPLIT_RE.split(text) if part.strip()] or [text]
    kinds = [_kind(part) for part in parts]
    named = [kind for kind in kinds if kind]
    if "lesson" in named and "office" in named:
        first = next(kind for kind in kinds if kind)
        task_parts: list[str] = []
        next_parts: list[str] = []
        seen_other = False
        for part, kind in zip(parts, kinds):
            if not seen_other and kind in {"", first}:
                task_parts.append(part)
            else:
                seen_other = True
                next_parts.append(part)
        return {
            "route": first,
            "task": "，".join(task_parts) or text,
            "next": "，".join(next_parts),
            "question": "",
        }
    kind = _kind(text)
    if kind:
        return {"route": kind, "task": text, "next": "", "question": ""}
    if text and _followup(text, previous):
        return {"route": previous, "task": text, "next": "", "question": ""}
    return {"route": "ask", "task": "", "next": "", "question": ASK_QUESTION}


def _parse_model(text: str, fallback: str) -> dict | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    route = str(data.get("route") or "")
    if route not in {"lesson", "office", "ask"}:
        return None
    task = str(data.get("task") or "").strip()
    question = str(data.get("question") or "").strip() or ASK_QUESTION
    if route in IDENTITIES and not task:
        task = fallback
    if route == "ask":
        task = ""
    return {
        "route": route,
        "task": task,
        "next": str(data.get("next") or "").strip(),
        "question": question if route == "ask" else "",
    }


async def _ask_model(content: str, previous: str | None, previous_text: str) -> str:
    payload = {
        "model": IDENTITIES["office"]["model"],
        "stream": False,
        "thinking": {"type": "disabled"},
        "max_tokens": 200,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你只负责分拣，不写正文。只输出一行 JSON。"
                    "route 只能是 lesson、office、ask。"
                    "lesson：教案、出题、组卷、答案、课件。"
                    "office：通知、家长沟通、会议纪要、润色。"
                    "接着上一轮就沿用 previous。"
                    "两边都像时，route 用先出现的那件，另一件原文放进 next。"
                    "看不出来就 ask，question 用一句中文。"
                    '格式：{"route":"lesson","task":"交给专家的原文","next":"","question":""}'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"previous={previous or '无'}\n"
                    f"上一句：{previous_text or '无'}\n"
                    f"这一句：{content}"
                ),
            },
        ],
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
        )
    if response.status_code != 200:
        LOG.warning("router http %s", response.status_code)
        return ""
    message = response.json()["choices"][0]["message"]
    return str(message.get("content") or "")


async def resolve_route(content: str, previous: str | None, previous_text: str = "") -> dict:
    """规则能定下来就直接用。只有看不出来时才问快速模型。"""
    local = decide(content, previous)
    if local["route"] != "ask" or not DEEPSEEK_API_KEY:
        return local
    try:
        parsed = _parse_model(await _ask_model(content, previous, previous_text), content)
    except Exception:
        LOG.warning("router classify failed", exc_info=True)
        return local
    return parsed or local
