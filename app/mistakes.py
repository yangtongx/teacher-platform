"""易错点只记知识点和错误类型，不记学生是谁。"""

from __future__ import annotations

import re

PERSONAL_RE = re.compile(
    r"身份证|姓名\s*[:：]|1[3-9]\d{9}|[\u4e00-\u9fff]{2,4}\s*\d{2,3}\s*分"
)


def personal_reason(text: str) -> str:
    if PERSONAL_RE.search(text or ""):
        return "只记知识点和错误类型，不要写学生姓名、分数和联系方式。"
    return ""
