"""主助手只做分拣，不出题、不写通知。

有模型时，先让快速模型输出一行 JSON，不做微调。
「再出一道」「好的」这类短跟进仍走本地规则。
模型没配、超时或 JSON 不合字段时，只追问一句，不再用关键词猜身份。
一句话里两件事时，先做先出现的那件。task 和 next 保持老师的原话。
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

# 只收意图词里常见的同音、近形错字。判断时用，不改正文。
_CONFUSABLE = {
    "知": "只",
    "卷": "券巻",
    "试": "式",
    "纪": "记",
    "题": "提",
    "案": "桉",
    "练": "连",
    "习": "系",
}


def _fuzzy(word: str) -> str:
    parts = []
    for char in word:
        alt = _CONFUSABLE.get(char, "")
        parts.append(f"[{char}{alt}]" if alt else re.escape(char))
    return "".join(parts)


_PAPER = "(?:" + "|".join(_fuzzy(word) for word in ("试卷", "试题", "习题")) + "|卷子|考卷)"
_LESSON_WORDS = (
    r"课件|PPT|ppt|幻灯片|组卷|命题|课时|单元测|参考答案|出一套|出一道|出几道|"
    r"分层作业|说课|教学反思|出卷|"
    + "|".join(_fuzzy(word) for word in ("教案", "出题", "练习题", "选择题", "填空题"))
    + "|"
    + _PAPER
    + r"|(?:出|做|组).{0,4}(?:卷子|考卷)"
)
LESSON_RE = re.compile(_LESSON_WORDS)
LESSON_ACTION_RE = re.compile(
    r"出一套|出一道|出几道|出卷|组卷|命题|分层作业|说课|教学反思|"
    + _fuzzy("出题")
    + r"|(?:写|做|设计).{0,4}(?:课件|PPT|ppt|"
    + _fuzzy("教案")
    + r")|(?:出|做|组|生成|编写|命制).{0,6}"
    + _PAPER
)
_OFFICE_WORDS = (
    _fuzzy("通知")
    + "|通告|"
    + _fuzzy("纪要")
    + r"|家长信|致家长|请柬|润色|发言稿|公示|家长会|家长沟通|会议记录|给家长|家长消息"
)
OFFICE_WORD_RE = re.compile(_OFFICE_WORDS)
OFFICE_ACTION_RE = OFFICE_WORD_RE
FOLLOW_RE = re.compile(r"^(再|继续|接着|沿用|还是|改|换一|这个|那个|上面|刚才|同样|也|好的|可以|行|嗯|对|不用|不需要)")
# 标点、连接词都切开。裸「再」不在这里切，避免把「再出一道」拆碎。
_BREAK_RE = re.compile(
    r"[，,、。．；;！!？?\n\r]+|(?:接下来|然后|之后|随后|接着|并且|同时|另外|还有|顺便|以及|再帮|还要)"
)
_TOPIC_BEFORE = re.compile(r"(关于|对于|有关)$")
_TOPIC_WORD = {"关于", "对于", "有关"}


def _lesson_action(text: str) -> bool:
    return bool(LESSON_ACTION_RE.search(text) or is_exam_request(text))


def _office_action(text: str) -> bool:
    return bool(OFFICE_ACTION_RE.search(text) or OFFICE_RE.search(text))


def _flags(text: str) -> tuple[bool, bool]:
    lesson = bool(LESSON_RE.search(text) or is_exam_request(text))
    office = bool(OFFICE_WORD_RE.search(text) or OFFICE_RE.search(text))
    return lesson, office


def _topic_before(text: str, pattern: re.Pattern[str]) -> bool:
    match = pattern.search(text)
    if not match:
        return False
    return bool(_TOPIC_BEFORE.search(text[: match.start()].rstrip()))


def _earlier_action(text: str) -> str:
    lesson_at = LESSON_ACTION_RE.search(text)
    office_at = OFFICE_ACTION_RE.search(text)
    if lesson_at and office_at:
        return "lesson" if lesson_at.start() <= office_at.start() else "office"
    if lesson_at or is_exam_request(text):
        return "lesson"
    return "office"


def _kind(text: str) -> str:
    lesson, office = _flags(text)
    if lesson and office:
        lesson_act = _lesson_action(text)
        office_act = _office_action(text)
        if lesson_act and not office_act:
            return "lesson"
        if office_act and not lesson_act:
            return "office"
        if lesson_act and office_act and _topic_before(text, OFFICE_ACTION_RE):
            return "lesson"
        if lesson_act and office_act and _topic_before(text, LESSON_ACTION_RE):
            return "office"
        if lesson_act and office_act:
            return _earlier_action(text)
        return "office"
    if lesson:
        return "lesson"
    if office:
        return "office"
    return ""


def _pure(kind: str, text: str) -> bool:
    lesson, office = _flags(text)
    if kind == "lesson":
        return lesson and not office
    return bool(kind == "office" and office and not lesson)


def _has_action(kind: str, text: str) -> bool:
    if kind == "lesson":
        return _lesson_action(text)
    return _office_action(text)


def _two_tasks(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_kind = _kind(left)
    right_kind = _kind(right)
    if not left_kind or not right_kind or left_kind == right_kind:
        return False
    if not _pure(left_kind, left) or not _pure(right_kind, right):
        return False
    return _has_action(left_kind, left) and _has_action(right_kind, right)


def _split_space(text: str) -> list[str]:
    bits = [bit for bit in re.split(r"\s+", text) if bit]
    if len(bits) < 2:
        return [text]
    kinds = [_kind(bit) for bit in bits]
    if "lesson" not in kinds or "office" not in kinds:
        return [text]
    for index, bit in enumerate(bits):
        if kinds[index] not in {"lesson", "office"}:
            continue
        previous = bits[index - 1] if index else ""
        if previous in _TOPIC_WORD or bit.startswith(tuple(_TOPIC_WORD)):
            return [text]
    groups = [bits[0]]
    current = kinds[0]
    out = []
    for bit, kind in zip(bits[1:], kinds[1:]):
        if kind and current and kind != current:
            out.append(" ".join(groups))
            groups = [bit]
            current = kind
        else:
            groups.append(bit)
            if kind:
                current = kind
    out.append(" ".join(groups))
    return out


def _action_cut(text: str) -> tuple[str, str] | None:
    points = []
    lesson_at = LESSON_ACTION_RE.search(text)
    office_at = OFFICE_ACTION_RE.search(text)
    if lesson_at:
        points.append((lesson_at.start(), "lesson"))
    if office_at:
        points.append((office_at.start(), "office"))
    if len({kind for _, kind in points}) < 2:
        return None
    points.sort()
    cut = points[1][0]
    if cut <= 0 or _TOPIC_BEFORE.search(text[:cut].rstrip()):
        return None
    return text[:cut].strip(), text[cut:].strip()


def _split_jammed(text: str) -> list[str]:
    for match in re.finditer("再", text):
        if match.start() == 0:
            continue
        left, right = text[: match.start()].strip(), text[match.end() :].strip()
        if _two_tasks(left, right):
            return [left, right]
    cut = _action_cut(text)
    if cut and _two_tasks(cut[0], cut[1]):
        return [cut[0], cut[1]]
    return [text]


def _clauses(text: str) -> list[str]:
    rough = [part.strip() for part in _BREAK_RE.split(text) if part.strip()] or [text]
    clauses: list[str] = []
    for part in rough:
        for piece in _split_space(part):
            clauses.extend(_split_jammed(piece))
    return clauses or [text]


def _followup(text: str, previous: str | None) -> bool:
    if previous not in IDENTITIES:
        return False
    if FOLLOW_RE.search(text):
        return True
    return len(text) <= 8


def decide(content: str, previous: str | None) -> dict:
    """不访问模型。route 为 lesson、office 或 ask。"""
    text = (content or "").strip()
    parts = _clauses(text)
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


def describe_files(files: list[dict] | None) -> str:
    """分类器看文件名、按正文判断的类型，以及不含姓名分数的一句结构说明。"""
    if not files:
        return "附件：无"
    lines = ["附件（类型按正文判断，文件名可能只是日期；没有学生姓名和分数）："]
    for item in files:
        name = str(item.get("filename") or "").replace("\n", " ").strip()
        kind = str(item.get("kind") or "")
        if kind not in {"成绩表", "大纲", "表格", "文档", "题目", "照片"}:
            kind = "文档"
        hint = str(item.get("hint") or "").replace("\n", " ").strip()
        if not name:
            continue
        if hint:
            lines.append(f"- {name}（{kind}：{hint}）")
        else:
            lines.append(f"- {name}（{kind}）")
    if len(lines) == 1:
        return "附件：无"
    return "\n".join(lines)


def _file_names(data: dict, known: set[str]) -> list[str]:
    raw = data.get("files") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    names = []
    for item in raw:
        name = str(item).replace("\n", " ").strip()
        if name in known and name not in names:
            names.append(name)
    return names


def _score_question(count: int) -> str:
    if count <= 1:
        return "这份表是考试成绩，出卷不会用到学生分数。要我按它看薄弱点，还是先只出卷？"
    return "这些表是考试成绩，出卷不会用到学生分数。要我放在一起看进退步，还是先只出卷？"


def guard_grades(decision: dict, briefs: list[dict] | None, content: str) -> dict:
    """出卷时，成绩表不论几份、文件名是什么，都不进题目。大纲跟着出卷走。"""
    grades = [str(item.get("filename") or "") for item in briefs or [] if item.get("kind") == "成绩表"]
    grades = [name for name in grades if name]
    text = (content or "").strip()
    if not grades or not (_lesson_action(text) or is_exam_request(text)):
        return decision
    if FOLLOW_RE.search(text):
        return decision
    decision = dict(decision)
    decision["files"] = grades
    if decision.get("route") != "lesson":
        decision["route"] = "lesson"
        decision["task"] = text
    nxt = str(decision.get("next") or "")
    if nxt and not re.search(r"成绩|对比|分数|薄弱", nxt):
        decision["question"] = ""
        return decision
    if re.search(r"对比|分析|进退步|薄弱", text):
        decision["question"] = ""
        if not nxt:
            decision["next"] = "处理这些考试成绩"
        return decision
    if not nxt:
        decision["next"] = "收下这些考试成绩"
    if not decision.get("question"):
        decision["question"] = _score_question(len(grades))
    return decision


def keep_exam_sources(decision: dict, briefs: list[dict] | None, content: str) -> dict:
    """手写题照片跟着出卷走。文件名像 IMG 也一样。不把题目拆成另一件办公的事。"""
    text = (content or "").strip()
    if FOLLOW_RE.search(text) or not (_lesson_action(text) or is_exam_request(text)):
        return decision
    mention = re.search(r"加上|算上|算进去|收进|一并|一起", text)
    names = []
    for item in briefs or []:
        kind = item.get("kind")
        if kind == "题目" or (kind == "照片" and mention):
            name = str(item.get("filename") or "")
            if name:
                names.append(name)
    if not names:
        return decision
    decision = dict(decision)
    if decision.get("route") != "lesson":
        decision["route"] = "lesson"
        decision["task"] = text
    nxt = str(decision.get("next") or "")
    if any(name in nxt for name in names) or re.search(r"照片|图片|手写|题目", nxt):
        decision["next"] = ""
        if "学生分数" not in str(decision.get("question") or ""):
            decision["question"] = ""
    decision["files"] = [name for name in decision.get("files") or [] if name not in names]
    return decision


def _parse_model(text: str, fallback: str, known: set[str] | None = None) -> dict | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    route = str(data.get("route") or "")
    if route not in {"lesson", "office", "ask"}:
        return None
    task = str(data.get("task") or "").strip()
    question = str(data.get("question") or "").strip()
    if route in IDENTITIES and not task:
        task = fallback
    if route == "ask":
        task = ""
        question = question or ASK_QUESTION
    return {
        "route": route,
        "task": task,
        "next": str(data.get("next") or "").strip(),
        "files": _file_names(data, known or set()),
        "question": question,
    }


def _answer_grade_question(text: str, previous_text: str, briefs: list[dict]) -> dict | None:
    """老师在回答要不要处理成绩。这句短回复不给出题专家。"""
    if "出卷不会用到学生分数" not in (previous_text or ""):
        return None
    names = [str(item.get("filename") or "") for item in briefs if item.get("kind") == "成绩表"]
    names = [name for name in names if name]
    if not names:
        return None
    if re.search(r"不|别|放着|只出", text):
        return {"route": "office", "task": "先不分析这些成绩表", "next": "", "files": names, "question": ""}
    if re.search(r"要|好|对比|分析|薄弱|进退步|嗯|行|看", text):
        return {"route": "office", "task": "处理这些考试成绩", "next": "", "files": names, "question": ""}
    return None


def _ask_fallback() -> dict:
    return {"route": "ask", "task": "", "next": "", "files": [], "question": ASK_QUESTION}


async def _ask_model(
    content: str,
    previous: str | None,
    previous_text: str,
    files: list[dict] | None = None,
) -> str:
    payload = {
        "model": IDENTITIES["office"]["model"],
        "stream": False,
        "thinking": {"type": "disabled"},
        "max_tokens": 400,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你只负责分拣，不写正文。只输出一行 JSON。"
                    "route 只能是 lesson、office、ask。"
                    "lesson：教案、出题、组卷、答案、课件。"
                    "office：通知、家长沟通、会议纪要、润色、成绩。"
                    "接着上一轮，或「再出一道」「好的」这类短跟进，route 用 previous。"
                    "没有标点也要拆。然后、再、接着后面若是另一件事，放进 next。"
                    "试券、通只这类错字按试卷、通知理解。task 和 next 用老师原话，不要改字。"
                    "附件类型按正文判断，文件名可能只是日期，不要凭文件名猜。"
                    "成绩表不论一份还是多份，都不是出卷材料。不要写学生姓名和分数。"
                    "老师要出卷，同时又有成绩表，且没说怎么用这些成绩："
                    "route 用 lesson，task 只留出卷那句，next 写收下这些考试成绩，"
                    "question 只问一句。一份就问要不要看薄弱点，多份再问要不要看进退步。"
                    "files 只填成绩表文件名。大纲、考试范围跟着出卷，question 留空，不要写进 files。"
                    "照片或扫描若是题目，也跟着出卷。老师说加上去、算进去，就把这些题收进这一套，"
                    "不要另开成办公的事，question 留空，files 不要填这些照片。"
                    "老师已经说了对比、分析或薄弱点，question 留空，next 写处理这些考试成绩。"
                    "看不出来就 ask，question 用一句中文。"
                    '格式：{"route":"lesson","task":"原话","next":"","files":[],"question":""}'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"previous={previous or '无'}\n"
                    f"上一句：{previous_text or '无'}\n"
                    f"这一句：{content}\n"
                    f"{describe_files(files)}"
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


async def resolve_route(
    content: str,
    previous: str | None,
    previous_text: str = "",
    files: list[dict] | None = None,
) -> dict:
    """有模型就先分类。短跟进走本地规则。模型失败就追问，不猜身份。"""
    text = (content or "").strip()
    briefs = files or []
    grade_answer = _answer_grade_question(text, previous_text, briefs)
    if grade_answer:
        return grade_answer
    if previous in IDENTITIES and _followup(text, previous) and not all(_flags(text)):
        return decide(text, previous)
    if not DEEPSEEK_API_KEY:
        return keep_exam_sources(guard_grades(decide(text, previous), briefs, text), briefs, text)
    known = {str(item.get("filename") or "") for item in briefs}
    try:
        parsed = _parse_model(
            await _ask_model(text, previous, previous_text, briefs),
            text,
            known,
        )
    except Exception:
        LOG.warning("router classify failed", exc_info=True)
        parsed = None
    if not parsed:
        return _ask_fallback()
    return keep_exam_sources(guard_grades(parsed, briefs, text), briefs, text)
