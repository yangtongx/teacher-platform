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
        "把学生卷和参考答案转成 Word，并附一份双向细目表。"
        "学生卷里不能出现答案、解析或难度。"
        "分层作业时 level 填基础、提高或拓展，同一范围分三次调用，每次一套。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "student_markdown": {"type": "string", "description": "学生卷 Markdown"},
            "answer_markdown": {"type": "string", "description": "教师答案 Markdown，每题含【知识点】和【难度】"},
            "level": {
                "type": "string",
                "description": "不分层就留空。分层时只能是基础、提高、拓展之一",
            },
        },
        "required": ["student_markdown", "answer_markdown"],
    }
    is_read_only = False

    async def call(
        self,
        student_markdown: str = "",
        answer_markdown: str = "",
        level: str = "",
        **_extra,
    ) -> object:
        turn = _teacher_lookup()
        if not turn or turn.get("identity") != "lesson":
            return _chunk("出卷请换到教案设计与出题专家。")
        try:
            files = exam.write_exam_files(
                turn["user"],
                int(turn["conversation_id"]),
                student_markdown,
                answer_markdown,
                level,
            )
        except ValueError as exc:
            return _chunk(str(exc))
        _publish(turn, files)
        lines = ["试卷已生成，只把下面的文件名和链接告诉老师，不要再粘贴题目。"]
        for item in files:
            lines.append(f"{item['name']} {item['url']}")
        if any(item.get("listening_note") for item in files):
            lines.append("听力 MP3 没有合成，原文在参考答案里。")
        return _chunk("\n".join(lines))


class ExportMaterials(_TeacherTool):
    name = "export_materials"
    description = (
        "把教案、说课稿、教学反思、通知或家长沟通稿收成 Word。"
        "documents 里每一份有 title 和 markdown。一次最多四份。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "documents": {
                "type": "array",
                "description": "要给老师下载的文稿",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "markdown": {"type": "string"},
                    },
                    "required": ["title", "markdown"],
                },
            }
        },
        "required": ["documents"],
    }
    is_read_only = False

    async def call(self, documents=None, **_extra) -> object:
        from app.docx import save_material

        turn = _teacher_lookup()
        if not turn:
            return _chunk("现在不能导出文稿。")
        items = documents if isinstance(documents, list) else []
        if not items:
            return _chunk("没有可导出的文稿。")
        if len(items) > 4:
            return _chunk("一次最多导出四份文稿。")
        files = []
        try:
            for item in items:
                if not isinstance(item, dict):
                    return _chunk("文稿格式不对，没有导出。")
                files.append(
                    save_material(
                        turn["user"],
                        int(turn["conversation_id"]),
                        str(item.get("title") or ""),
                        str(item.get("markdown") or ""),
                    )
                )
        except ValueError as exc:
            return _chunk(str(exc))
        _publish(turn, files)
        lines = ["文稿已生成，把文件名和链接告诉老师。"]
        for item in files:
            lines.append(f"{item['name']} {item['url']}")
        return _chunk("\n".join(lines))


class SaveMistakes(_TeacherTool):
    name = "save_mistakes"
    description = "记下这次的易错知识点，供下次出变式。不要写学生姓名、分数和联系方式。"
    input_schema = {
        "type": "object",
        "properties": {
            "notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "description": "知识点"},
                        "detail": {"type": "string", "description": "错在哪里"},
                    },
                    "required": ["topic", "detail"],
                },
            }
        },
        "required": ["notes"],
    }
    is_read_only = False

    async def call(self, notes=None, **_extra) -> object:
        from app import db
        from app.mistakes import personal_reason

        turn = _teacher_lookup()
        if not turn or turn.get("identity") != "lesson":
            return _chunk("易错点请交给教案设计与出题专家。")
        items = notes if isinstance(notes, list) else []
        blocked = []
        kept = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = f"{item.get('topic') or ''} {item.get('detail') or ''}"
            reason = personal_reason(text)
            if reason:
                blocked.append(reason)
                continue
            kept.append(item)
        if blocked and not kept:
            return _chunk(blocked[0])
        saved = db.save_mistake_notes(
            int(turn["user"]["id"]),
            int(turn["conversation_id"]),
            kept,
        )
        if not saved:
            return _chunk("没有记下易错点。请写知识点和错误类型。")
        tail = "有一条因为写了学生信息，没有保存。" if blocked else "下次出题会针对这些点出变式。"
        return _chunk(f"已记下 {saved} 条易错点。{tail}")


class PptStyle(_TeacherTool):
    name = "ppt_style"
    description = "做课件前先调用。它会说直接用科目样式、先问老师一次，或只确认上一回的样式。"
    input_schema = {"type": "object", "properties": {}}
    is_read_only = True

    async def call(self, **_extra) -> object:
        from app.ppt_style import describe_style

        turn = _teacher_lookup()
        if not turn or turn.get("identity") != "lesson":
            return _chunk("课件请换到教案设计与出题专家。")
        return _chunk(describe_style(turn["user"], int(turn["conversation_id"])))


class ExportPpt(_TeacherTool):
    name = "export_ppt"
    description = (
        "把一课时课件写成 PPT。"
        "slides 每一页含 role、title、bullets、source。"
        "role 只能是课题与目标、导入、新知、例题、练习、小结、作业。"
        "source 只能是教案、课本或要求。新知不能只写要求。"
        "每页最多 5 条短句。第一页必须是课题与目标，并且要有小结和作业。"
        "use 填 keep、subject 或 upload。subject 只填语文、数学、英语或通用，留空则用老师账号上的科目。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "课题，用来做文件名"},
            "use": {
                "type": "string",
                "description": "keep 沿用上次，subject 用科目样式，upload 用老师刚上传的课件",
            },
            "subject": {
                "type": "string",
                "description": "语文、数学、英语或通用。留空则用账号上的科目",
            },
            "slides": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "title": {"type": "string"},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                        "source": {"type": "string", "description": "教案、课本或要求"},
                        "notes": {"type": "string", "description": "只给老师看的备注，可空"},
                    },
                    "required": ["role", "title", "bullets", "source"],
                },
            },
        },
        "required": ["title", "slides"],
    }
    is_read_only = False

    async def call(self, title: str = "", slides=None, use: str = "", subject: str = "", **_extra) -> object:
        from app.ppt import write_ppt
        from app.ppt_style import StyleNeeded, prepare_export

        turn = _teacher_lookup()
        if not turn or turn.get("identity") != "lesson":
            return _chunk("课件请换到教案设计与出题专家。")
        items = slides if isinstance(slides, list) else []
        try:
            template, subject_name = prepare_export(
                turn["user"],
                int(turn["conversation_id"]),
                use,
                subject,
            )
        except StyleNeeded as exc:
            return _chunk(str(exc))
        tries = int(turn.get("ppt_tries") or 0) + 1
        turn["ppt_tries"] = tries
        try:
            files = [
                write_ppt(
                    turn["user"],
                    int(turn["conversation_id"]),
                    title,
                    items,
                    template,
                    subject_name,
                )
            ]
        except ValueError as exc:
            if tries >= 2:
                return _chunk(f"已经改过一次仍不能导出。不要再调用 export_ppt。告诉老师：{exc}")
            return _chunk(str(exc))
        _publish(turn, files)
        lines = ["课件已生成，只把文件名和链接告诉老师，不要再粘贴每页内容。"]
        for item in files:
            lines.append(f"{item['name']} {item['url']}")
        return _chunk("\n".join(lines))


class ReviewParentNote(_TeacherTool):
    name = "review_parent_note"
    description = "检查给家长的文字里有没有刺激人或拿学生做比较的说法。成文前先调用。"
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "准备发给家长的全文"},
        },
        "required": ["text"],
    }
    is_read_only = True

    async def call(self, text: str = "", **_extra) -> object:
        from app.tone import parent_flags

        found = parent_flags(text)
        if not found:
            return _chunk("措辞可以。请再给老师一段约 80 字的短版，和一段把事情写完整的长版。")
        words = "、".join(found)
        return _chunk(f"这些说法建议改掉：{words}。改完再分成短版和长版。")


def _publish(turn: dict, files: list) -> None:
    """同一轮里多次导出时，下载都留在这一条回复下。"""
    if turn.get("files_ready"):
        turn["files"] = list(turn.get("files") or []) + list(files)
    else:
        turn["files"] = list(files)
    turn["files_ready"] = True


def build_toolkit(identity: str):
    ns = _import_framework()
    catalog = {
        "AskUser": lambda: ns["AskUser"](),
        "list_textbooks": ListTextbooks,
        "search_textbooks": SearchTextbooks,
        "export_exam": ExportExam,
        "export_materials": ExportMaterials,
        "ppt_style": PptStyle,
        "export_ppt": ExportPpt,
        "save_mistakes": SaveMistakes,
        "review_parent_note": ReviewParentNote,
    }
    tools = [catalog[name]() for name in specialist(identity)["tools"]]
    skills = [
        ns["LocalSkillLoader"](directory=str(folder), scan_subdir=True)
        for folder in skill_directories(identity)
    ]
    return ns["Toolkit"](tools=tools, skills_or_loaders=skills)
