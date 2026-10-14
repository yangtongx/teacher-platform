"""专家登记表。主助手只从这里挑选，不在别处再写一份名单。"""

from __future__ import annotations

from pathlib import Path

from app.agents.lesson import IDENTITY as LESSON_ID
from app.agents.lesson import NAME as LESSON_NAME
from app.agents.lesson import PROMPT as LESSON_PROMPT
from app.agents.lesson import SKILLS as LESSON_SKILLS
from app.agents.lesson import TOOLS as LESSON_TOOLS
from app.agents.office import IDENTITY as OFFICE_ID
from app.agents.office import NAME as OFFICE_NAME
from app.agents.office import PROMPT as OFFICE_PROMPT
from app.agents.office import SKILLS as OFFICE_SKILLS
from app.agents.office import TOOLS as OFFICE_TOOLS
from app.config import ROOT

SPECIALISTS = {
    LESSON_ID: {
        "name": LESSON_NAME,
        "prompt": LESSON_PROMPT,
        "skills": LESSON_SKILLS,
        "tools": LESSON_TOOLS,
        "status": "正在出题",
    },
    OFFICE_ID: {
        "name": OFFICE_NAME,
        "prompt": OFFICE_PROMPT,
        "skills": OFFICE_SKILLS,
        "tools": OFFICE_TOOLS,
        "status": "正在写",
    },
}

ALLOWED_TOOLS = frozenset(name for spec in SPECIALISTS.values() for name in spec["tools"])


def specialist(identity: str) -> dict:
    return SPECIALISTS[identity]


def skill_directories(identity: str) -> list[Path]:
    """只加载该专家声明的技能。上传、飞书和音频脚本不进模型上下文。"""
    spec = SPECIALISTS.get(identity)
    if not spec or not spec["skills"]:
        return []
    base = ROOT / "workspaces" / "lesson" / "skills"
    return [base / name for name in spec["skills"] if (base / name).is_dir()]


def tool_call_allowed(name: str, identity: str | None = None) -> bool:
    if name not in ALLOWED_TOOLS:
        return False
    if not identity:
        return name != "export_exam"
    spec = SPECIALISTS.get(identity)
    if spec is None:
        return False
    return name in spec["tools"]
