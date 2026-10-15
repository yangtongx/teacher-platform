"""口令哈希。存成 pbkdf2_sha256$迭代次数$盐$摘要，和原来的教师登录同一格式。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

# 与现有教师登录服务同一套存储格式，便于以后迁入口令哈希。
PBKDF2_ALGO = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600_000
PBKDF2_SALT_BYTES = 16
PBKDF2_MAX_ITERATIONS = 5_000_000


def _b64_encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64_decode(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS, salt: bytes | None = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{PBKDF2_ALGO}${iterations}${_b64_encode(salt)}${_b64_encode(digest)}"


def parse_password_hash(stored: str) -> tuple[int, bytes, bytes] | None:
    if not isinstance(stored, str):
        return None
    parts = stored.split("$")
    if len(parts) != 4:
        return None
    algo, iterations_text, salt_text, hash_text = parts
    if algo != PBKDF2_ALGO:
        return None
    try:
        iterations = int(iterations_text)
    except (TypeError, ValueError):
        return None
    if iterations <= 0 or iterations > PBKDF2_MAX_ITERATIONS:
        return None
    try:
        salt = _b64_decode(salt_text)
        expected = _b64_decode(hash_text)
    except (binascii.Error, ValueError):
        return None
    if not salt or not expected:
        return None
    return iterations, salt, expected


def verify_password(password: str, stored: str) -> bool:
    parsed = parse_password_hash(stored)
    if parsed is None:
        return False
    iterations, salt, expected = parsed
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
