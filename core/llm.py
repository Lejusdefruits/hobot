"""Shared chat entry point for the whole project -- the actual model is
picked once by core/llm_provider.py (Ollama by default, or a cloud key via
LLM_PROVIDER); nothing here knows or cares which provider answered.

The semaphore enforces LLM_MAX_CONCURRENT (set in .env) regardless of
provider: several LangGraph nodes scoring offers in parallel never push a
local Ollama model past this limit. A cloud provider has its own rate limits
and doesn't strictly need this, but capping concurrency here is harmless
either way and keeps one code path for both.
"""
import base64
import json
import os
import re
import threading

from langchain_core.messages import HumanMessage
from langchain_core.messages.content import create_image_block

from core.llm_provider import get_chat_model

MAX_CONCURRENT = int(os.environ.get("LLM_MAX_CONCURRENT", "2"))

_semaphore = threading.Semaphore(MAX_CONCURRENT)


def extract_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError(f"No usable JSON in the model's response:\n{raw[:500]}")
        return json.loads(match.group(0))


def _to_langchain_message(m: dict) -> HumanMessage:
    """Converts the ollama-style dict callers already build ({"role": ...,
    "content": ..., "images": [png bytes, ...]}) into LangChain's standard
    message/content-block format -- every caller (core/profile.py,
    tools/cv_tailor.py) keeps passing raw PNG bytes exactly as before, this is
    the only place that needs to know the target shape. create_image_block()
    is the SAME standard block langchain-openai/langchain-anthropic consume
    too, so vision works identically across all three providers with no
    provider-specific branching here."""
    if not m.get("images"):
        return HumanMessage(content=m["content"])
    blocks = [{"type": "text", "text": m["content"]}]
    blocks += [
        create_image_block(base64=base64.b64encode(img).decode("ascii"), mime_type="image/png")
        for img in m["images"]
    ]
    return HumanMessage(content=blocks)


def chat(messages: list[dict], temperature: float = 0.1) -> str:
    lc_messages = [_to_langchain_message(m) for m in messages]
    with _semaphore:
        response = get_chat_model(temperature=temperature).invoke(lc_messages)
    return response.content


def chat_json(prompt: str, images: list[bytes] | None = None, temperature: float = 0.1) -> dict:
    message = {"role": "user", "content": prompt}
    if images:
        message["images"] = images
    return extract_json(chat([message], temperature=temperature))
