"""各专家共用的执行循环。提示词和工具由 registry 里的专家自己声明。"""

from __future__ import annotations

import asyncio
import contextvars
import json

from app import db
from app.agents.registry import specialist, tool_call_allowed
from app.agents.tools import _import_framework, bind_teacher, build_toolkit
from app.config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, IDENTITIES

_teacher = contextvars.ContextVar("teacher_turn", default=None)
bind_teacher(_teacher.get)


def _credential(ns: dict):
    fields = getattr(ns["DeepSeekCredential"], "model_fields", {})
    kwargs = {"api_key": DEEPSEEK_API_KEY}
    if "base_url" in fields and DEEPSEEK_BASE_URL:
        kwargs["base_url"] = DEEPSEEK_BASE_URL
    return ns["DeepSeekCredential"](**kwargs)


def _model(ns: dict, identity: str):
    parameters_cls = getattr(ns["DeepSeekChatModel"], "Parameters", None)
    parameters = None
    if parameters_cls is not None:
        fields = getattr(parameters_cls, "model_fields", {})
        data = {}
        if "thinking_enable" in fields:
            data["thinking_enable"] = False
        if "max_tokens" in fields:
            data["max_tokens"] = 8192
        if data:
            parameters = parameters_cls(**data)
    kwargs = {
        "credential": _credential(ns),
        "model": IDENTITIES[identity]["model"],
        "stream": True,
    }
    if parameters is not None:
        kwargs["parameters"] = parameters
    return ns["DeepSeekChatModel"](**kwargs)


def _state_from_row(ns: dict, row) -> object:
    if row is not None and row["state_json"]:
        return ns["AgentState"].model_validate_json(row["state_json"])
    return ns["AgentState"](
        permission_context=ns["PermissionContext"](mode=ns["PermissionMode"].BYPASS)
    )


def _build(user, identity: str, conversation_id: int):
    ns = _import_framework()
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("还没有配置 DEEPSEEK_API_KEY，所以现在不能生成回复。")
    spec = specialist(identity)
    row = db.load_agent_session(int(user["id"]), conversation_id, identity)
    agent = ns["Agent"](
        name=spec["name"],
        system_prompt=spec["prompt"],
        model=_model(ns, identity),
        toolkit=build_toolkit(identity),
        state=_state_from_row(ns, row),
        react_config=ns["ReActConfig"](max_iters=16, stop_on_reject=True),
    )
    return ns, agent, row


def _dump_state(agent) -> str:
    state = agent.state
    if hasattr(state, "model_dump_json"):
        return state.model_dump_json()
    return json.dumps(state, ensure_ascii=False)


def _save(user, identity: str, conversation_id: int, agent, pending, files) -> None:
    db.save_agent_session(
        int(user["id"]),
        conversation_id,
        identity,
        _dump_state(agent),
        json.dumps(pending, ensure_ascii=False) if pending else None,
        json.dumps(files, ensure_ascii=False) if files else None,
    )


def _fresh_files() -> list:
    """只有这一轮刚导出的文件才交给老师。以前已经给过的下载不要再挂到新回复下面。"""
    ctx = _teacher.get() or {}
    if not ctx.get("files_ready"):
        return []
    found = ctx.get("files") or []
    return list(found) if isinstance(found, list) else []


def public_view(user_id: int, identity: str) -> dict:
    conversation_id = db.conversation_id(user_id, identity)
    row = db.load_agent_session(user_id, conversation_id)
    if row is None:
        return {"pending": None, "files": []}
    pending = json.loads(row["pending_json"]) if row["pending_json"] else None
    if pending:
        pending = {key: pending[key] for key in ("questions",) if key in pending}
    return {"pending": pending, "files": []}


def _questions(tool_input) -> list[dict]:
    data = json.loads(tool_input) if isinstance(tool_input, str) else dict(tool_input or {})
    found = []
    for item in data.get("questions") or []:
        if not isinstance(item, dict):
            continue
        options = []
        for option in item.get("options") or []:
            if isinstance(option, dict) and option.get("label"):
                options.append(
                    {
                        "label": str(option.get("label") or ""),
                        "description": str(option.get("description") or ""),
                    }
                )
        found.append(
            {
                "question": str(item.get("question") or ""),
                "header": str(item.get("header") or ""),
                "context": str(item.get("context") or ""),
                "options": options,
            }
        )
    return found


def _tool_name(call) -> str:
    for attr in ("name", "tool_name"):
        value = getattr(call, attr, None)
        if isinstance(value, str) and value:
            return value
    function = getattr(call, "function", None)
    if function is not None:
        value = getattr(function, "name", None)
        if isinstance(value, str) and value:
            return value
    return ""


def _allow(ns: dict, event):
    results = []
    for call in event.tool_calls:
        turn = _teacher.get() or {}
        identity = turn.get("identity") if isinstance(turn, dict) else None
        allowed = tool_call_allowed(_tool_name(call), identity)
        results.append(
            ns["ConfirmResult"](
                confirmed=allowed,
                tool_call=call,
                rules=[],
            )
        )
    return ns["UserConfirmResultEvent"](reply_id=event.reply_id, confirm_results=results)


def _resume_event(ns: dict, pending: dict, answers: list, skip: bool):
    questions = pending.get("questions") or []
    packed = []
    lines = []
    for index, question in enumerate(questions):
        answer = answers[index] if index < len(answers) and isinstance(answers[index], dict) else {}
        selected = answer.get("selected") if isinstance(answer.get("selected"), list) else []
        selected = [str(item) for item in selected if str(item).strip()]
        other = str(answer.get("other") or "").strip()
        if skip and not selected and not other:
            other = "老师跳过了。按推荐的默认继续，不要再问同一个问题。"
        label = "、".join(selected) if selected else other or "未回答"
        lines.append(f"{question.get('question') or '问题'}：{label}")
        packed.append(
            ns["AskUserAnswer"](
                question=question.get("question") or "",
                selected=selected,
                other=other or None,
            )
        )
    block = ns["ToolResultBlock"](
        id=pending["tool_call_id"],
        name="AskUser",
        output=[ns["TextBlock"](type="text", text="\n".join(lines) or "老师没有选择。")],
        metadata=ns["AskUserMetadata"](answers=packed).model_dump(),
        state=ns["ToolResultState"].SUCCESS,
    )
    return ns["ExternalExecutionResultEvent"](
        reply_id=pending["reply_id"],
        execution_results=[block],
    )


async def respond(
    user,
    identity: str,
    content: str | None = None,
    answers: list | None = None,
    skip: bool = False,
    conversation_id: int | None = None,
):
    """跑一轮。新问题传 content；老师回答了 AskUser 就传 answers。"""
    ns = _import_framework()
    if conversation_id is None:
        conversation_id = db.conversation_id(int(user["id"]), identity)
    ns, agent, row = _build(user, identity, conversation_id)
    pending = json.loads(row["pending_json"]) if row is not None and row["pending_json"] else None
    stored = json.loads(row["files_json"]) if row is not None and row["files_json"] else []
    # 出卷后如果还停在提问上，这些文件还没写进消息，回答完再给一次。
    owe_files = bool(pending) and isinstance(stored, list) and bool(stored)
    if answers is not None or (skip and pending):
        if not pending:
            yield {"error": "现在没有待确认的问题。"}
            return
        incoming = _resume_event(ns, pending, answers or [], skip)
    else:
        if pending:
            answers = [{"other": content or ""}]
            incoming = _resume_event(ns, pending, answers, False)
        else:
            body = content or ""
            uploads = db.upload_context(int(user["id"]), identity, conversation=conversation_id)
            if uploads:
                body = body + "\n\n" + uploads
            incoming = ns["UserMsg"](name=str(user["display_name"] or "老师"), content=body)
    token = _teacher.set(
        {
            "user": user,
            "identity": identity,
            "conversation_id": conversation_id,
            "goal": content or "",
            "files": list(stored) if owe_files else [],
            "files_ready": owe_files,
        }
    )
    try:
        yielded = False
        final_text = ""
        yield {"status": specialist(identity)["status"]}
        async for event in _drive(ns, agent, incoming):
            if event["kind"] == "status":
                yield {"status": event["text"]}
            elif event["kind"] == "delta":
                yielded = True
                final_text += event["text"]
                yield {"delta": event["text"]}
            elif event["kind"] == "ask":
                pending_out = event["pending"]
                _save(user, identity, conversation_id, agent, pending_out, _fresh_files() or None)
                yield {"ask": {"questions": pending_out["questions"]}}
                return
            elif event["kind"] == "final":
                final_text = event["text"] or final_text
            elif event["kind"] == "error":
                _save(user, identity, conversation_id, agent, None, None)
                yield {"error": event["text"]}
                return
        files = _fresh_files()
        message_id = None
        if final_text.strip():
            message_id = db.add_message(
                int(user["id"]),
                identity,
                "assistant",
                final_text.strip(),
                files or None,
                conversation=conversation_id,
            )
        elif not yielded and files:
            final_text = "试卷已生成，请下载试卷和参考答案。"
            message_id = db.add_message(
                int(user["id"]), identity, "assistant", final_text, files, conversation=conversation_id
            )
            yield {"delta": final_text}
        _save(user, identity, conversation_id, agent, None, None)
        if files:
            yield {"files": files}
        yield {"done": True, "message_id": message_id}
    except asyncio.CancelledError:
        files = _fresh_files()
        if final_text.strip() or files:
            text = final_text.strip() or "试卷已生成，请下载试卷和参考答案。"
            db.add_message(
                int(user["id"]), identity, "assistant", text, files or None, conversation=conversation_id
            )
        _save(user, identity, conversation_id, agent, None, None)
        raise
    finally:
        _teacher.reset(token)


async def _drive(ns: dict, agent, incoming):
    try:
        stream = agent.reply_stream(incoming, yield_final_msg=True)
    except TypeError:
        stream = agent.reply_stream(incoming)
    async for event in stream:
        name = type(event).__name__
        if name == "RequireUserConfirmEvent":
            async for item in _drive(ns, agent, _allow(ns, event)):
                yield item
            return
        if name == "RequireExternalExecutionEvent":
            call = event.tool_calls[0]
            tool_input = call.input
            yield {
                "kind": "ask",
                "pending": {
                    "reply_id": event.reply_id,
                    "tool_call_id": call.id,
                    "questions": _questions(tool_input),
                },
            }
            return
        if name == "Msg" or hasattr(event, "get_text_content"):
            text = event.get_text_content() or ""
            reason = getattr(event, "finished_reason", None)
            reason_name = getattr(reason, "value", reason)
            if reason_name in {"error", "ERROR"} or str(reason_name).lower() == "error":
                detail = getattr(event, "error", None)
                yield {"kind": "error", "text": _safe_error(detail or text)}
                return
            if text:
                yield {"kind": "final", "text": text}
            continue
        if name == "ModelCallStartEvent":
            yield {"kind": "status", "text": "正在出题"}
            continue
        if name == "ThinkingBlockStartEvent":
            yield {"kind": "status", "text": "正在整理题目"}
            continue
        if name == "ToolCallStartEvent":
            yield {"kind": "status", "text": _tool_status(getattr(event, "tool_call_name", ""))}
            continue
        if "Thinking" in name or name.startswith("Tool"):
            continue
        if name != "TextBlockDeltaEvent":
            continue
        delta = getattr(event, "delta", None)
        if isinstance(delta, str) and delta:
            yield {"kind": "delta", "text": delta}


def _tool_status(name: str) -> str:
    labels = {
        "list_textbooks": "正在查看教材",
        "search_textbooks": "正在检索教材",
        "export_exam": "正在生成试卷",
        "export_materials": "正在整理文稿",
        "export_ppt": "正在做课件",
        "ppt_style": "正在确认课件样式",
        "save_mistakes": "正在记下易错点",
        "review_parent_note": "正在看措辞",
        "AskUser": "正在整理问题",
    }
    if name in labels:
        return labels[name]
    if "skill" in name.lower():
        return "正在读出题规范"
    return "正在处理"


def _safe_error(detail) -> str:
    text = str(detail or "")
    if "402" in text or "Insufficient" in text or "余额" in text:
        return "DeepSeek 余额不足。充值后重新发送即可。"
    return "助手这一步没有完成，请再发一次。"
