"""Shared Ollama client -- a single entry point to the local model for the
whole project.

The semaphore enforces LLM_MAX_CONCURRENT (set in .env) regardless of caller:
several LangGraph nodes scoring offers in parallel never push the GPU/CPU
past this limit.
"""
import json
import os
import re
import threading
from pathlib import Path

import ollama
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.8")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MAX_CONCURRENT = int(os.environ.get("LLM_MAX_CONCURRENT", "2"))

_client = ollama.Client(host=OLLAMA_HOST)
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


def chat(messages: list[dict], temperature: float = 0.1) -> str:
    with _semaphore:
        response = _client.chat(model=OLLAMA_MODEL, messages=messages, options={"temperature": temperature})
    return response["message"]["content"]


def chat_json(prompt: str, images: list[bytes] | None = None, temperature: float = 0.1) -> dict:
    message = {"role": "user", "content": prompt}
    if images:
        message["images"] = images
    return extract_json(chat([message], temperature=temperature))
