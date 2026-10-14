import io
import os
import tempfile
import unittest
import zipfile

if not os.environ.get("DATA_DIR"):
    os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_USERNAME", "teacher")
os.environ.setdefault("ADMIN_PASSWORD", "test-password-1")
os.environ.setdefault("ADMIN_DISPLAY_NAME", "测试老师")

from app import db
from app.auth import hash_password
from app.extract import extract_text


class HardeningTests(unittest.TestCase):
    def setUp(self):
        db.init_db()

    def tearDown(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM rate_events")
            conn.commit()

    def test_docs_are_closed_and_responses_are_framed(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        self.assertEqual(client.get("/docs").status_code, 404)
        self.assertEqual(client.get("/openapi.json").status_code, 404)
        health = client.get("/healthz")
        self.assertEqual(health.headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", health.headers["content-security-policy"])
        self.assertEqual(health.headers["x-content-type-options"], "nosniff")

    def test_null_origin_on_same_site_logout_reaches_login(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        response = client.post(
            "/logout",
            headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")
        self.assertEqual(
            client.get("/healthz").headers["referrer-policy"],
            "strict-origin-when-cross-origin",
        )

    def test_cross_origin_post_is_rejected(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        response = client.post(
            "/login",
            data={"username": "teacher", "password": "test-password-1"},
            headers={"Origin": "https://evil.example"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)

    def test_null_origin_from_another_site_is_rejected(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        response = client.post(
            "/logout",
            headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)

    def test_password_change_drops_the_session(self):
        user = db.user_by_username("teacher")
        token = db.create_session(user["id"], "lesson")
        self.assertIsNotNone(db.session_user(token))
        db.set_password_hash(user["id"], hash_password("another-password"))
        self.assertIsNone(db.session_user(token))
        db.set_password_hash(user["id"], hash_password("test-password-1"))

    def test_login_lockout_uses_the_same_message_channel(self):
        from fastapi.testclient import TestClient

        from app.main import app

        os.environ["LOGIN_FAIL_LIMIT"] = "2"
        try:
            client = TestClient(app)
            for _ in range(2):
                failed = client.post(
                    "/login",
                    data={"username": "teacher", "password": "wrong-password"},
                    follow_redirects=False,
                )
                self.assertEqual(failed.status_code, 303)
                self.assertIn("error=1", failed.headers["location"])
            locked = client.post(
                "/login",
                data={"username": "teacher", "password": "test-password-1"},
                follow_redirects=False,
            )
            self.assertEqual(locked.status_code, 303)
            self.assertIn("error=2", locked.headers["location"])
        finally:
            os.environ.pop("LOGIN_FAIL_LIMIT", None)

    def test_download_expires(self):
        from fastapi.testclient import TestClient

        from app.main import app

        user = db.user_by_username("teacher")
        db.add_message(user["id"], "office", "assistant", "一份通知")
        message = db.list_messages(user["id"], "office")[-1]
        db.save_document(user["id"], message["id"], "expired-token", "通知.docx")
        db.save_document(user["id"], message["id"], "fresh-token", "通知.docx")
        docs = db.DATA_DIR / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "expired-token.docx").write_bytes(b"expired")
        (docs / "fresh-token.docx").write_bytes(b"fresh")
        with db.connect() as conn:
            conn.execute(
                "UPDATE documents SET expires_at = ? WHERE token = ?",
                ("2000-01-01T00:00:00+00:00", "expired-token"),
            )
            conn.commit()
        client = TestClient(app)
        logged = client.post(
            "/login",
            data={"username": "teacher", "password": "test-password-1"},
            follow_redirects=False,
        )
        self.assertEqual(logged.status_code, 303)
        self.assertEqual(client.get("/files/fresh-token").status_code, 200)
        self.assertEqual(client.get("/files/expired-token").status_code, 404)

    def test_oversized_docx_is_rejected(self):
        from app import extract

        previous = extract.MAX_UNCOMPRESSED
        extract.MAX_UNCOMPRESSED = 50
        try:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("word/document.xml", b"a" * 80)
            with self.assertRaises(ValueError):
                extract_text("big.docx", buffer.getvalue())
        finally:
            extract.MAX_UNCOMPRESSED = previous

    def test_lesson_skills_leave_out_cloud_and_feishu(self):
        from app.agents.registry import skill_directories, tool_call_allowed
        names = [path.name for path in skill_directories("lesson")]
        self.assertEqual(names, ["exam-generator", "markdown-to-exam-docx"])
        self.assertEqual(skill_directories("office"), [])
        self.assertTrue(tool_call_allowed("export_exam", "lesson"))
        self.assertFalse(tool_call_allowed("export_exam", "office"))
        self.assertFalse(tool_call_allowed("bash"))
        self.assertFalse(tool_call_allowed(""))

    def test_router_sends_exam_and_notice_to_different_agents(self):
        from fastapi.testclient import TestClient

        from app.main import app

        seen = []

        async def fake_respond(user, identity, content=None, answers=None, skip=False, conversation_id=None):
            seen.append((identity, content))
            yield {"delta": identity}
            yield {"done": True, "message_id": 1}

        from unittest.mock import patch

        import app.agents.conductor as conductor
        from app.agents import router as agent_router
        from app.agents.router import decide

        agent_router.DEEPSEEK_API_KEY = ""
        self.assertEqual(decide("再出一道", "lesson")["route"], "lesson")
        mixed = decide("出一套试卷，再写一份家长通知", None)
        self.assertEqual(mixed["route"], "lesson")
        self.assertIn("通知", mixed["next"])

        client = TestClient(app)
        logged = client.post(
            "/login",
            data={"username": "teacher", "password": "test-password-1"},
            follow_redirects=False,
        )
        self.assertEqual(logged.status_code, 303)
        with patch.object(conductor, "_respond", fake_respond):
            exam_reply = client.post("/api/chat", json={"content": "帮我出一套期中试卷"})
            notice_reply = client.post("/api/chat", json={"content": "写一份家长会通知"})
        self.assertEqual(exam_reply.status_code, 200)
        self.assertEqual(notice_reply.status_code, 200)
        self.assertEqual(seen[0][0], "lesson")
        self.assertEqual(seen[1][0], "office")
        self.assertIn("试卷", seen[0][1])
        ask = client.post("/api/chat", json={"content": "你好"})
        self.assertEqual(ask.status_code, 200)
        self.assertIn("出题", ask.text)
        self.assertEqual(len(seen), 2)

    def test_office_cannot_export_an_exam(self):
        from app.exam import write_exam_files

        user = db.user_by_username("teacher")
        conversation_id = db.conversation_id(user["id"], "office")
        db.add_message(user["id"], "office", "user", "出一套题")
        with self.assertRaises(ValueError):
            write_exam_files(user, conversation_id, "学生卷", "【答案】B")

    def test_roster_import_keeps_an_existing_password(self):
        user = db.user_by_username("teacher")
        previous = user["password_hash"]
        token = db.create_session(user["id"], "lesson")
        try:
            db.import_accounts([
                {
                    "username": "teacher",
                    "display_name": "测试老师",
                    "password_hash": hash_password("roster-would-replace"),
                    "role": "admin",
                    "disabled": False,
                    "assigned_scopes": [],
                }
            ])
            updated = db.user_by_username("teacher")
            self.assertEqual(updated["password_hash"], previous)
            self.assertIsNotNone(db.session_user(token))
            db.import_accounts([
                {
                    "username": "teacher",
                    "display_name": "测试老师",
                    "password_hash": previous,
                    "role": "admin",
                    "disabled": True,
                    "assigned_scopes": [],
                }
            ])
            self.assertIsNone(db.session_user(token))
        finally:
            db.save_account(user["id"], "teacher", "测试老师", "admin", False, [], "")

    def test_expired_session_is_rejected(self):
        user = db.user_by_username("teacher")
        token = db.create_session(user["id"], "lesson")
        with db.connect() as conn:
            conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE token = ?",
                ("2000-01-01T00:00:00+00:00", token),
            )
            conn.commit()
        self.assertIsNone(db.session_user(token))

    def test_disabling_an_account_drops_its_session(self):
        admin = db.user_by_username("teacher")
        db.create_account("lin2", "林老师", "password-ok", "teacher", [])
        lin = db.user_by_username("lin2")
        token = db.create_session(lin["id"], "lesson")
        db.save_account(admin["id"], "lin2", "林老师", "teacher", True, [], "")
        self.assertIsNone(db.session_user(token))


if __name__ == "__main__":
    unittest.main()
