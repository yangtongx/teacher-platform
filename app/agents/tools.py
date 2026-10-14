"""专家共用的工具。谁能调用哪一个，由 registry 里的专家声明决定。"""

from __future__ import annotations

from agentscope.tool import ToolBase

from app import exam
from app.agents.registry import skill_directories, specialist

_teacher_lookup = None


def bind_teacher(getter):
    """执行循环把当前老师放进上下文，工具从这里读取。"""
    global _teacher_lookup
    _teacher_lookup = getter


def _import_framework():
    from agentscope.agent import Agent, ReActConfig
    from agentscope.credential import DeepSeekCredential
    from agentscope.event import (
        ExternalExecutionResultEvent,
        RequireExternalExecutionEvent,
        RequireUserConfirmEvent,
        UserConfirmResultEvent,
        ConfirmResult,
    )
    from agentscope.message import (
        TextBlock,
        ToolResultBlock,
        ToolResultState,
        UserMsg,
    )
    from agentscope.model import DeepSeekChatModel
    from agentscope.permission import (
        PermissionBehavior,
        PermissionContext,
        PermissionDecision,
        PermissionMode,
    )
    from agentscope.skill import LocalSkillLoader
    from agentscope.state import AgentState
    from agentscope.tool import AskUser, AskUserAnswer, AskUserMetadata, ToolBase, ToolChunk, Toolkit

    return locals()


def _chunk(text: str):
    ns = _import_framework()
    return ns["ToolChunk"](content=[ns["TextBlock"](type="text", text=text)])


class _TeacherTool(ToolBase):
    """查教材、导出试卷。这些动作直接放行，不再向老师确认一次工具本身。"""

    is_concurrency_safe = False
    is_external_tool = False

    async def check_permissions(self, tool_input, context):
        ns = _import_framework()
        return ns["PermissionDecision"](
            behavior=ns["PermissionBehavior"].ALLOW,
            message="教师助手自己的工具",
        )


class ListTextbooks(_TeacherTool):
    name = "list_textbooks"
    description = "查看这位老师被授权的教材，以及各有多少块素材。"
    input_schema = {"type": "object", "properties": {}, "required": []}
    is_read_only = True

    async def call(self, **_extra) -> object:
        from app.corpus import textbook_dbs

        turn = _teacher_lookup()
        books = textbook_dbs(turn["user"])
        text = exam.confirm_line(books) if books else "这个账号还没有对上的教材。"
        return _chunk(text)


class SearchTextbooks(_TeacherTool):
    name = "search_textbooks"
    description = "在这位老师的教材里检索考试范围。query 写要考的章节或主题，不要只写一个词。"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要检索的范围，写成一句完整的话"},
        },
        "required": ["query"],
    }
    is_read_only = True

    async def call(self, query: str = "", **_extra) -> object:
        from app.corpus import lesson_excerpts

        turn = _teacher_lookup()
        text = lesson_excerpts(turn["user"], query)
        if not text:
            text = "没有检索到教材原文。不要编造课文、页码和课标，必要时用 AskUser 问老师怎么继续。"
        return _chunk(text)


class ExportExam(_TeacherTool):
    name = "export_exam"
    description = (
        "把学生卷和参考答案转成两份 Word，并给出下载链接。"
        "学生卷里不能出现答案、解析或难度。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "student_markdown": {"type": "string", "description": "学生卷 Markdown"},
            "answer_markdown": {"type": "string", "description": "教师答案 Markdown"},
        },
        "required": ["student_markdown", "answer_markdown"],
    }
    is_read_only = False

    async def call(self, student_markdown: str = "", answer_markdown: str = "", **_extra) -> object:
        turn = _teacher_lookup()
        if not turn or turn.get("identity") != "lesson":
            return _chunk("出卷请换到教案设计与出题专家。")
        try:
            files = exam.write_exam_files(
                turn["user"],
                int(turn["conversation_id"]),
                student_markdown,
                answer_markdown,
            )
        except ValueError as exc:
            return _chunk(str(exc))
        turn["files"] = files
        lines = ["试卷已生成，只把下面的文件名和链接告诉老师，不要再粘贴题目。"]
        for item in files:
            lines.append(f"{item['name']} {item['url']}")
        if any(item.get("listening_note") for item in files):
            lines.append("听力 MP3 没有合成，原文在参考答案里。")
        return _chunk("\n".join(lines))


def build_toolkit(identity: str):
    ns = _import_framework()
    catalog = {
        "AskUser": lambda: ns["AskUser"](),
        "list_textbooks": ListTextbooks,
        "search_textbooks": SearchTextbooks,
        "export_exam": ExportExam,
    }
    tools = [catalog[name]() for name in specialist(identity)["tools"]]
    skills = [
        ns["LocalSkillLoader"](directory=str(folder), scan_subdir=True)
        for folder in skill_directories(identity)
    ]
    return ns["Toolkit"](tools=tools, skills_or_loaders=skills)
