"""Picks which chat model actually answers -- Ollama (local, default) or a
cloud provider, selected once here by LLM_PROVIDER in .env. Every LLM call
site in the project (core/llm.py's chat()/chat_json(), and
graphs/chat_agent.py's ReAct agent) goes through this one factory, so adding
a provider means touching one file, not every caller.

Each branch imports its own langchain-* package lazily so a user who only
ever runs Ollama (the default, and the only truly free option) never needs
langchain-openai/langchain-anthropic installed at all -- a hard top-level
import of either would turn an optional feature into a mandatory dependency.
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama").lower()

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.8")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL") or None  # override to hit any OpenAI-compatible endpoint

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")


def get_chat_model(temperature: float = 0.1) -> BaseChatModel:
    if LLM_PROVIDER == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=OLLAMA_MODEL, base_url=OLLAMA_HOST, temperature=temperature)

    if LLM_PROVIDER == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=OPENAI_MODEL, api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL, temperature=temperature)

    if LLM_PROVIDER == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=ANTHROPIC_MODEL, api_key=ANTHROPIC_API_KEY, temperature=temperature)

    raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r} (expected ollama, openai, or anthropic)")
