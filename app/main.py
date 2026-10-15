"""教师助手的 HTTP 入口。

页面、登录和对话接口都在这里。老师发来的话不在这里写回复，
交给 app.agents.conductor，由它分给出题专家或办公助手。
会改数据的请求先核对来源：退出时浏览器可能把 Origin 写成 null，
同源请求仍应回到登录页，而不是报「请求来源不对」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import db, exam
from app.auth import hash_password, verify_password
from app.config import (
    DATA_DIR,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    THREAD,
    chat_limit,
    chat_window_seconds,
    cookie_secure,
    login_fail_limit,
    login_window_seconds,
)
from app.corpus import account_scope_ids, catalog_choices, known_scope_ids, lesson_excerpts, textbook_notice
from app.docx import document_name, write_docx
from app.extract import OCR_NOTE, extract_text, safe_filename
from app.llm import ModelError, stream_reply

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_UPLOADS = 8

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
LOG = logging.getLogger("teacher-platform")

app = FastAPI(title="教师助手", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if peer in {"127.0.0.1", "::1"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip() if forwarded else ""
        if first:
            return first[:64]
    return (peer or "unknown")[:64]


def _host_key(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    if text.startswith("[") and "]" in text:
        host = text[1:text.index("]")]
        rest = text[text.index("]") + 1 :]
        if rest in {"", ":80", ":443"}:
            return host
        return f"{host}{rest}"
    if text.count(":") == 1:
        host, port = text.rsplit(":", 1)
        if port in {"80", "443"}:
            return host
    return text


def _origin_allowed(request: Request) -> bool:
    """同源表单才放行。页面若声明不发送来源，浏览器会把 Origin 写成 null。"""
    host = request.headers.get("host", "")
    origin = (request.headers.get("origin") or "").strip()
    site = request.headers.get("sec-fetch-site", "")
    if origin == "null":
        return site == "same-origin"
    if not host or not origin:
        return True
    allowed = {_host_key(host)}
    forwarded = request.headers.get("x-forwarded-host", "")
    for item in forwarded.split(","):
        item = item.strip()
        if item:
            allowed.add(_host_key(item))
    origin_host = _host_key(urlparse(origin).netloc)
    if origin_host in allowed:
        return True
    LOG.info("origin rejected host=%s origin=%s site=%s", host, origin, site or "-")
    return False


def _secure_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self'; img-src 'self' data:; font-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if cookie_secure():
        response.headers["Strict-Transport-Security"] = "max-age=15552000; includeSubDomains"
    return response


@app.middleware("http")
async def protect(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _origin_allowed(request):
        return _secure_headers(JSONResponse({"error": "请求来源不对"}, status_code=403))
    response = await call_next(request)
    return _secure_headers(response)


@app.on_event("startup")
def startup() -> None:
    db.init_db()


def current(request: Request):
    """返回会话令牌，以及（用户, 当前身份）。未登录时身份为空。"""
    token = request.cookies.get(SESSION_COOKIE, "")
    return token, db.session_user(token)


# 登录页和健康检查。容器只探 /healthz，不走登录。
@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/")
def index(request: Request):
    token, session = current(request)
    if session is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC / "app.html", headers={"Cache-Control": "no-cache"})


@app.get("/login")
def login_page(request: Request):
    _, session = current(request)
    if session is not None:
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC / "login.html")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    user = db.user_by_username(username) if username else None
    ip = _client_ip(request)
    bucket = f"login:{ip}"
    if db.rate_count(bucket, login_window_seconds()) >= login_fail_limit():
        return RedirectResponse("/login?error=2", status_code=303)
    password_ok = False
    if user is not None and not user["disabled"]:
        password_ok = await asyncio.to_thread(verify_password, password, user["password_hash"])
    if user is None or user["disabled"] or not password_ok:
        db.rate_hit(bucket)
        db.audit(None, "login_fail", username[:64])
        return RedirectResponse("/login?error=1", status_code=303)
    token = db.create_session(int(user["id"]), THREAD)
    db.audit(int(user["id"]), "login", user["username"])
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(),
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )
    return response


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        _, session = current(request)
        if session is not None:
            user, _identity = session
            db.audit(int(user["id"]), "logout", user["username"])
        db.delete_session(token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# 当前对话、消息和教材提示。聊天走 conductor，不在这个文件里生成正文。
@app.get("/api/state")
def state(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _identity = session
    return {
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
        "identity": THREAD,
        "identities": [],
        "notice": textbook_notice(user),
        **_thread(int(user["id"]), THREAD),
    }


@app.post("/api/chat")
async def chat(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _identity = session
    body = await request.json()
    content = str(body.get("content") or "").strip()
    if not content:
        return JSONResponse({"error": "请输入内容"}, status_code=400)
    if len(content) > 8000:
        return JSONResponse({"error": "这条太长了"}, status_code=400)
    user_id = int(user["id"])
    if not _spend_chat(user_id):
        return JSONResponse({"error": "这一小时里的请求太多了，请稍后再试。"}, status_code=429)

    async def events():
        try:
            from app.agents.conductor import handle_turn

            async for item in handle_turn(user, content):
                yield _sse(item)
        except Exception as exc:
            yield _sse({"error": _agent_error(exc)})

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/export")
async def export_word(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _ = session
    body = await request.json()
    try:
        message_id = int(body.get("message_id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "没有这条回复"}, status_code=400)
    message = db.message_for_user(int(user["id"]), message_id)
    if message is None or message["role"] != "assistant" or not str(message["content"]).strip():
        return JSONResponse({"error": "没有这条回复"}, status_code=404)
    token = secrets.token_urlsafe(16)
    filename = document_name(message["content"]) + ".docx"
    write_docx(message["content"], DATA_DIR / "docs" / f"{token}.docx")
    db.save_document(int(user["id"]), message_id, token, filename)
    db.audit(int(user["id"]), "download", filename)
    return {"url": f"/files/{token}", "filename": filename}


@app.get("/files/{token}")
def download_word(token: str, request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _ = session
    document = db.document_for_user(int(user["id"]), token)
    suffix = Path(str(document["filename"] if document else "")).suffix.lower()
    if suffix not in {".docx", ".pptx"}:
        suffix = ".docx"
    path = DATA_DIR / "docs" / f"{token}{suffix}"
    if document is None or not path.is_file():
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    db.audit(int(user["id"]), "download", document["filename"])
    kinds = {
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    return FileResponse(
        path,
        media_type=kinds[suffix],
        filename=document["filename"],
    )


@app.post("/api/password")
async def change_password(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _ = session
    body = await request.json()
    current_password = str(body.get("current") or "")
    new_password = str(body.get("password") or "")
    password_ok = await asyncio.to_thread(verify_password, current_password, user["password_hash"])
    if not password_ok:
        return JSONResponse({"error": "当前口令不对"}, status_code=400)
    if len(new_password) < 8:
        return JSONResponse({"error": "新口令至少 8 位"}, status_code=400)
    db.set_password_hash(int(user["id"]), hash_password(new_password))
    db.audit(int(user["id"]), "password_change", user["username"])
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# 左侧对话：新建、打开、改名、删除。删除时会清掉消息和已生成的文件。
@app.post("/api/conversations")
def new_conversation(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _identity = session
    db.create_conversation(int(user["id"]), THREAD)
    return _thread(int(user["id"]), THREAD)


@app.post("/api/conversations/open")
async def open_conversation(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _identity = session
    body = await request.json()
    try:
        conversation = int(body.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "没有这段对话"}, status_code=400)
    if not db.open_conversation(int(user["id"]), THREAD, conversation):
        return JSONResponse({"error": "没有这段对话"}, status_code=404)
    return _thread(int(user["id"]), THREAD)


@app.post("/api/conversations/rename")
async def rename_conversation(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _identity = session
    body = await request.json()
    try:
        conversation = int(body.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "没有这段对话"}, status_code=400)
    if not db.rename_conversation(int(user["id"]), conversation, str(body.get("title") or "")):
        return JSONResponse({"error": "这段对话没法改名"}, status_code=400)
    return _thread(int(user["id"]), THREAD)


@app.post("/api/conversations/delete")
async def remove_conversations(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _identity = session
    body = await request.json()
    raw = body.get("ids")
    if not isinstance(raw, list) or not raw:
        return JSONResponse({"error": "没有要删除的对话"}, status_code=400)
    if not db.delete_conversations(int(user["id"]), raw):
        return JSONResponse({"error": "没有这段对话"}, status_code=404)
    return _thread(int(user["id"]), THREAD)


@app.post("/api/uploads")
async def upload_document(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _identity = session
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "请选择文档"}, status_code=400)
    data = await upload.read()
    if not data:
        return JSONResponse({"error": "文件是空的"}, status_code=400)
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "文件超过 8MB"}, status_code=400)
    try:
        filename = safe_filename(getattr(upload, "filename", "") or "")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    user_id = int(user["id"])
    if db.upload_count(user_id, THREAD) >= MAX_UPLOADS:
        return JSONResponse({"error": "这段对话最多 8 个文档"}, status_code=400)
    if Path(filename).suffix.lower() in {".pptx", ".potx"}:
        try:
            from app.ppt_style import store_pptx

            store_pptx(user_id, filename, data)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {
            "uploads": db.list_uploads(user_id, THREAD),
            "notice": "这份课件只用来记样式，不会把里面的旧内容抄进新课件。",
        }
    try:
        suffix = Path(filename).suffix.lower()
        if suffix in {".csv", ".xlsx", ".xlsm", ".xls"}:
            from app.extract import read_table

            text = read_table(filename, data)
        else:
            text = extract_text(filename, data)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    db.add_upload(user_id, THREAD, filename, text)
    payload = {"uploads": db.list_uploads(user_id, THREAD)}
    if text.startswith(OCR_NOTE):
        payload["notice"] = "图片或扫描页已经识别成文字，请核对后再用。"
    return payload


@app.post("/api/uploads/delete")
async def remove_upload(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _identity = session
    body = await request.json()
    try:
        upload_id = int(body.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "没有这份文档"}, status_code=400)
    if not db.delete_upload(int(user["id"]), upload_id):
        return JSONResponse({"error": "没有这份文档"}, status_code=404)
    return {"uploads": db.list_uploads(int(user["id"]), THREAD)}


# 管理员维护教师账号，以及按教材范围勾选这位老师能检索的书。
@app.get("/accounts")
def accounts_page(request: Request):
    _, session = current(request)
    if session is None:
        return RedirectResponse("/login", status_code=302)
    user, _ = session
    if not db.is_admin(user):
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC / "accounts.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/accounts")
def accounts(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _ = session
    if not db.is_admin(user):
        return JSONResponse({"error": "只有管理员能管账号"}, status_code=403)
    choices = catalog_choices()
    labels = {item["id"]: _scope_label(item) for item in choices}
    return {
        "me": user["username"],
        "catalog": choices,
        "accounts": [_public_account(row, labels) for row in db.list_accounts()],
    }


@app.post("/api/accounts")
async def create_account(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _ = session
    if not db.is_admin(user):
        return JSONResponse({"error": "只有管理员能管账号"}, status_code=403)
    body = await request.json()
    scopes, error = _checked_scopes(body.get("scopes"))
    if error:
        return JSONResponse({"error": error}, status_code=400)
    try:
        db.create_account(
            str(body.get("username") or "").strip(),
            str(body.get("display_name") or "").strip(),
            str(body.get("password") or ""),
            str(body.get("role") or "teacher"),
            scopes,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    db.audit(int(user["id"]), "account_create", str(body.get("username") or "").strip())
    return {"ok": True}


@app.post("/api/accounts/save")
async def save_account(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _ = session
    if not db.is_admin(user):
        return JSONResponse({"error": "只有管理员能管账号"}, status_code=403)
    body = await request.json()
    scopes, error = _checked_scopes(body.get("scopes"))
    if error:
        return JSONResponse({"error": error}, status_code=400)
    try:
        db.save_account(
            int(user["id"]),
            str(body.get("username") or "").strip(),
            str(body.get("display_name") or "").strip(),
            str(body.get("role") or "teacher"),
            bool(body.get("disabled")),
            scopes,
            str(body.get("password") or ""),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    db.audit(int(user["id"]), "account_save", str(body.get("username") or "").strip())
    return {"ok": True}


@app.post("/api/exams/start")
async def begin_exam(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _identity = session
    body = await request.json()
    try:
        exam_id = int(body.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "没有这份试卷"}, status_code=400)
    user_id = int(user["id"])
    row = db.exam_for_user(user_id, exam_id)
    if row is None or row["status"] != "asking":
        return JSONResponse({"error": "这份试卷不在待确认状态"}, status_code=400)
    owned = db.conversation_owner(int(row["conversation_id"]))
    if owned is None or int(owned["user_id"]) != user_id:
        return JSONResponse({"error": "没有这份试卷"}, status_code=404)
    if db.running_exam(user_id):
        return JSONResponse({"error": "上一份试卷还在生成，完成后再出下一套。"}, status_code=400)
    if not _spend_chat(user_id):
        return JSONResponse({"error": "这一小时里的请求太多了，请稍后再试。"}, status_code=429)
    try:
        listening, difficulty = exam.take_answers(row, body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not db.start_exam(user_id, exam_id, listening, difficulty):
        return JSONResponse({"error": "这份试卷不在待确认状态"}, status_code=400)
    exam.schedule(exam_id)
    fresh = db.exam_for_user(user_id, exam_id)
    return exam.public_exam(fresh, user)


@app.get("/api/exams/{exam_id}")
def exam_status(exam_id: int, request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _identity = session
    row = db.exam_for_user(int(user["id"]), exam_id)
    if row is None:
        return JSONResponse({"error": "没有这份试卷"}, status_code=404)
    return exam.public_exam(row, user)


@app.post("/api/agent/answer")
async def agent_answer(request: Request):
    _, session = current(request)
    if session is None:
        return JSONResponse({"error": "未登录"}, status_code=401)
    user, _identity = session
    body = await request.json()
    answers = body.get("answers") if isinstance(body.get("answers"), list) else []
    if not _spend_chat(int(user["id"])):
        return JSONResponse({"error": "这一小时里的请求太多了，请稍后再试。"}, status_code=429)

    async def events():
        try:
            from app.agents.conductor import resume_turn

            async for item in resume_turn(user, answers, skip=bool(body.get("skip"))):
                yield _sse(item)
        except Exception as exc:
            yield _sse({"error": _agent_error(exc)})

    return StreamingResponse(events(), media_type="text/event-stream")


def _agent_error(exc: Exception) -> str:
    LOG.exception("agent failed")
    text = str(exc)
    if "DEEPSEEK_API_KEY" in text or "api_key" in text.lower():
        return "模型没有配置好。"
    if "402" in text or "余额" in text or "Insufficient" in text:
        return "DeepSeek 余额不足。充值后重新发送即可。"
    if "No module named" in text and "agentscope" in text:
        return "AgentScope 还没有装好。"
    return "助手中断了，请再发一次。"


def _spend_chat(user_id: int) -> bool:
    bucket = f"chat:{user_id}"
    if db.rate_count(bucket, chat_window_seconds()) >= chat_limit():
        return False
    db.rate_hit(bucket)
    return True


def _thread(user_id: int, identity: str) -> dict:
    user = db.user_by_id(user_id)
    cards = [exam.public_exam(row, user) for row in db.list_exams(user_id, identity)] if user else []
    try:
        from app.agents.runtime import public_view

        agent_view = public_view(user_id, identity)
    except Exception:
        agent_view = {"pending": None, "files": []}
    return {
        "conversation_id": db.conversation_id(user_id, identity),
        "conversations": db.list_conversations(user_id, identity),
        "messages": db.list_messages(user_id, identity),
        "uploads": db.list_uploads(user_id, identity),
        "exams": cards,
        "agent": agent_view,
    }


def _checked_scopes(value: object) -> tuple[list[str], str]:
    if value is None:
        value = []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return [], "教材范围不对"
    scopes = list(dict.fromkeys(value))
    known = known_scope_ids()
    if scopes and not known:
        return [], "教材目录读不出来，暂时不能改范围"
    missing = [item for item in scopes if item not in known]
    if missing:
        return [], "有的教材不在目录里"
    return scopes, ""


def _scope_label(item: dict) -> str:
    return f"{item['stage']}{item['subject']}{item['version']} {item['label']}"


def _public_account(row, labels: dict[str, str]) -> dict:
    scopes = account_scope_ids(row)
    return {
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "disabled": bool(row["disabled"]),
        "scopes": [{"id": item, "label": labels.get(item, item)} for item in scopes],
    }


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
