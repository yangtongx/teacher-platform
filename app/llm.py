from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

from app.config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, IDENTITIES


class ModelError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


LOG = logging.getLogger("teacher-platform")


async def stream_reply(identity: str, history: list[dict]) -> AsyncIterator[str]:
    spec = IDENTITIES[identity]
    if not DEEPSEEK_API_KEY:
        raise ModelError("还没有配置 DEEPSEEK_API_KEY，所以现在不能生成回复。")
    payload = {
        "model": spec["model"],
        "stream": True,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "system", "content": spec["system"]}, *history],
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(120.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
        ) as response:
            if response.status_code == 402:
                raise ModelError("DeepSeek 余额不足。充值后重新发送即可。")
            if response.status_code == 401:
                raise ModelError("DeepSeek 密钥无效。")
            if response.status_code != 200:
                LOG.warning("deepseek http %s", response.status_code)
                raise ModelError("模型请求失败。")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content") or ""
                if delta:
                    yield delta


async def complete_text(model: str, system: str, user: str, *, max_tokens: int = 8192) -> str:
    """一次拿回整段文字。出卷用，不把思考过程当正文。"""
    if not DEEPSEEK_API_KEY:
        raise ModelError("还没有配置 DEEPSEEK_API_KEY，所以现在不能生成回复。")
    payload = {
        "model": model,
        "stream": False,
        "thinking": {"type": "disabled"},
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(180.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
        )
    if response.status_code == 402:
        raise ModelError("DeepSeek 余额不足。充值后重新发送即可。")
    if response.status_code == 401:
        raise ModelError("DeepSeek 密钥无效。")
    if response.status_code != 200:
        LOG.warning("deepseek http %s", response.status_code)
        raise ModelError("模型请求失败。")
    try:
        message = response.json()["choices"][0]["message"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ModelError("模型没有返回试卷。") from exc
    text = str(message.get("content") or "").strip()
    if not text:
        raise ModelError("模型没有写成试卷，请再出一次。")
    return text
