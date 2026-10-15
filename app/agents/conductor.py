"""主助手接到老师的话以后，交给出题专家或办公助手。"""

from __future__ import annotations

from app import db
from app.agents.registry import SPECIALISTS
from app.agents.router import resolve_route
from app.config import THREAD

LABELS = {key: spec["name"] for key, spec in SPECIALISTS.items()}


def _respond(*args, **kwargs):
    # 推迟导入，测试分拣时不必把 AgentScope 一起装上。
    from app.agents.runtime import respond

    return respond(*args, **kwargs)


def _other(identity: str) -> str:
    return "office" if identity == "lesson" else "lesson"


def _with_notes(user_id: int, conversation_id: int, identity: str, task: str) -> str:
    """出题前带上这段对话里已经记下的易错点。里面没有学生姓名。"""
    if identity != "lesson" or not task:
        return task
    notes = db.list_mistake_notes(user_id, conversation_id)
    if not notes:
        return task
    lines = [f"- {item['topic']}：{item['detail']}" for item in notes]
    return task + "\n\n这段对话记下的易错点，出题时针对它们出变式，不要写出学生姓名：\n" + "\n".join(lines)


def _pack_followup(decision: dict) -> dict | None:
    question = str(decision.get("question") or "").strip()
    nxt = str(decision.get("next") or "").strip()
    if not question and not nxt:
        return None
    return {"question": question, "next": nxt}


async def _release_followup(user, user_id: int, conversation_id: int, identity: str):
    """前一件做完后再处理后一件。要问成绩对比时，先问，不直接分析。"""
    follow = db.pop_followup(conversation_id)
    if not follow:
        return
    question = follow.get("question") or ""
    if question:
        message_id = db.add_message(
            user_id, THREAD, "assistant", question, conversation=conversation_id
        )
        yield {"delta": "\n" + question}
        yield {"done": True, "message_id": message_id}
        return
    nxt = follow.get("next") or ""
    if not nxt:
        return
    other = _other(identity)
    yield {"status": f"正在交给{LABELS.get(other, '助手')}"}
    async for item in _respond(user, other, nxt, conversation_id=conversation_id):
        yield item


async def handle_turn(user, content: str):
    """保存老师的话，分拣后交给对应身份。一句话里两件事就按先后各做一件。"""
    user_id = int(user["id"])
    conversation_id = db.conversation_id(user_id, THREAD)
    pending = db.pending_agent(user_id, conversation_id)
    db.add_message(user_id, THREAD, "user", content, conversation=conversation_id)
    if pending:
        asked = False
        async for item in _respond(
            user, pending, content, conversation_id=conversation_id
        ):
            if item.get("ask"):
                asked = True
            yield item
        if not asked:
            async for item in _release_followup(user, user_id, conversation_id, pending):
                yield item
        return
    history = db.recent_history(user_id, THREAD, limit=6)
    previous_text = ""
    if len(history) >= 2:
        previous_text = history[-2]["content"]
    db.set_followup(conversation_id, None)
    decision = await resolve_route(
        content,
        db.last_agent(user_id, conversation_id),
        previous_text,
        db.upload_briefs(conversation_id),
    )
    if decision["route"] == "ask":
        question = decision["question"]
        message_id = db.add_message(
            user_id, THREAD, "assistant", question, conversation=conversation_id
        )
        yield {"delta": question}
        yield {"done": True, "message_id": message_id}
        return
    identity = decision["route"]
    follow = _pack_followup(decision)
    if follow:
        db.set_followup(conversation_id, follow)
    yield {"status": f"正在交给{LABELS.get(identity, '助手')}"}
    asked = False
    async for item in _respond(
        user,
        identity,
        _with_notes(user_id, conversation_id, identity, decision["task"] or content),
        conversation_id=conversation_id,
    ):
        if item.get("ask"):
            asked = True
        yield item
    if asked:
        return
    async for item in _release_followup(user, user_id, conversation_id, identity):
        yield item


async def resume_turn(user, answers: list | None, skip: bool = False):
    """老师回答了身份里的问题，回到当时那个身份继续。"""
    user_id = int(user["id"])
    conversation_id = db.conversation_id(user_id, THREAD)
    identity = db.pending_agent(user_id, conversation_id)
    if not identity:
        yield {"error": "现在没有待确认的问题。"}
        return
    asked = False
    async for item in _respond(
        user,
        identity,
        None,
        answers,
        skip,
        conversation_id=conversation_id,
    ):
        if item.get("ask"):
            asked = True
        yield item
    if asked:
        return
    async for item in _release_followup(user, user_id, conversation_id, identity):
        yield item
