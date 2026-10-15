"""家长沟通里容易刺激人的说法。只点出词语，不改写整段。"""

from __future__ import annotations

import re

HARSH = (
    "差生",
    "笨",
    "不听话",
    "拖后腿",
    "太让人失望",
    "问题学生",
    "屡教不改",
    "成绩太差",
    "别人家",
    "别的孩子",
    "垫底",
    "倒数",
)
COMPARE_RE = re.compile(r"比.{0,8}(同学|学生|孩子).{0,6}(差|好|强|弱)")


def parent_flags(text: str) -> list[str]:
    found = []
    raw = text or ""
    for phrase in HARSH:
        if phrase in raw and phrase not in found:
            found.append(phrase)
    if COMPARE_RE.search(raw) and "和别的学生比" not in found:
        found.append("和别的学生比")
    return found
