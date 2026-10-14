"""主助手接到老师的话以后，交给出题专家或办公助手。"""

from __future__ import annotations

from app import db
from app.agents.registry import SPECIALISTS
from app.agents.router import resolve_route
from app.config import THREAD

LABELS = {key: spec["name"] for key, spec in SPECIALISTS.items()}


def _respond(*args, **kwargs):
    from app.agents.runtime import respond

    return respond(*args, **kwargs)


def _other(identity: str) -> str:
    return "office" if identity == "lesson" else "lesson"


async def handle_turn(user, content: str):
    """保存老师的话，分拣后交给对应身份。一句话里两件事就按先后各做一件。"""
    user_id = int(user["id"])
    conversation_id = db.conversation_id(user_id, THREAD)
    pending = db.pending_agent(user_id, conversation_id)
    db.add_message(user_id, THREAD, "user", content, conversation=conversation_id)
    if pending:
        async for item in _respond(
            user, pending, content, conversation_id=conversation_id
        ):
            yield item
        return
    history = db.recent_history(user_id, THREAD, limit=6)
    previous_text = ""
    if len(history) >= 2:
        previous_text = history[-2]["content"]
    decision = await resolve_route(content, db.last_agent(user_id, conversation_id), previous_text)
    if decision["route"] == "ask":
        question = decision["question"]
        message_id = db.add_message(
            user_id, THREAD, "assistant", question, conversation=conversation_id
        )
        yield {"delta": question}
        yield {"done": True, "message_id": message_id}
        return
    identity = decision["route"]
    yield {"status": f"正在交给{LABELS.get(identity, '助手')}"}
    asked = False
    async for item in _respond(
        user, identity, decision["task"] or content, conversation_id=conversation_id
    ):
        if item.get("ask"):
            asked = True
        yield item
    if asked or not decision.get("next"):
        return
    nxt = _other(identity)
    yield {"status": f"正在交给{LABELS.get(nxt, '助手')}"}
    async for item in _respond(
        user, nxt, decision["next"], conversation_id=conversation_id
    ):
        yield item


async def resume_turn(user, answers: list | None, skip: bool = False):
    """老师回答了身份里的问题，回到当时那个身份继续。"""
    user_id = int(user["id"])
    conversation_id = db.conversation_id(user_id, THREAD)
    identity = db.pending_agent(user_id, conversation_id)
    if not identity:
        yield {"error": "现在没有待确认的问题。"}
        return
    async for item in _respond(
        user,
        identity,
        None,
        answers,
        skip,
        conversation_id=conversation_id,
    ):
        yield item
