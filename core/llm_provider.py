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
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama").lower()

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.8")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# Without this, Ollama picks its own context window (server default, or
# whatever it auto-shrinks to fit available VRAM) -- confirmed in practice to
# come in well under what this agent actually needs: the system prompt plus
# ~37 tool schemas (graphs/chat_agent.py) alone measured at over 6000 tokens
# on a real call, before a single token of conversation history (trimmed to
# CHAT_AGENT_MAX_CONTEXT_TOKENS, 6000 by default) or the model's own
# "thinking" (qwen3.8 is a reasoning model) is added. When the real prompt
# doesn't fit, Ollama silently truncates it instead of erroring -- observed
# directly to corrupt which tools the model even sees (it invented a tool
# name that doesn't exist in TOOLS) and to leave it so little room that the
# final answer comes back empty or cut off mid-thought. 20000 comfortably
# covers system prompt + tools + full history + thinking + a real answer;
# raise it further for a much longer CHAT_AGENT_MAX_CONTEXT_TOKENS, lower it
# only if your hardware can't spare the extra VRAM for the larger KV cache.
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "20000"))

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL") or None  # override to hit any OpenAI-compatible endpoint

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")


@lru_cache(maxsize=None)
def get_chat_model(temperature: float = 0.1) -> BaseChatModel:
    """Cached per temperature (the only thing that varies call to call --
    provider/model/host/key are all fixed for the process's lifetime, read
    from .env once above): a discovery run can call this dozens of times
    scoring offers, and building a fresh ChatOpenAI/ChatAnthropic/ChatOllama
    each time discarded connection pooling for no reason -- these are
    stateless, thread-safe request builders (LLM_MAX_CONCURRENT already caps
    how many calls run at once), safe to hand out the same instance
    repeatedly."""
    return _build_chat_model(temperature)


def _build_chat_model(temperature: float) -> BaseChatModel:
    if LLM_PROVIDER == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=OLLAMA_MODEL, base_url=OLLAMA_HOST, temperature=temperature, num_ctx=OLLAMA_NUM_CTX,
        )

    if LLM_PROVIDER == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=OPENAI_MODEL, api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL, temperature=temperature)

    if LLM_PROVIDER == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=ANTHROPIC_MODEL, api_key=ANTHROPIC_API_KEY, temperature=temperature)

    raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r} (expected ollama, openai, or anthropic)")
