"""
Общая сборка LLM/STT движков из env — используется всеми боевыми точками входа
(run_production.py, scheduler.py, run_local_audio.py), чтобы они не расходились
в приоритете провайдеров и не забывали передать нужные kwargs (было: scheduler.py
падал на STT_MODE=route, т.к. не собирал deepgram_key/elevenlabs_key).
"""
from __future__ import annotations
import os
from typing import Any

from .llm_client import BaseLLMClient, AnthropicClient, GeminiClient, MockClient
from .stt import STTEngine, get_engine as _stt_engine


def build_llm(env: dict[str, str] | None = None) -> tuple[BaseLLMClient, str]:
    """Gemini (бесплатный тир) в приоритете, потом Anthropic, иначе mock."""
    env = env if env is not None else os.environ
    if env.get("GEMINI_API_KEY"):
        return GeminiClient(), "Gemini"
    if env.get("ANTHROPIC_API_KEY"):
        return AnthropicClient(), "Anthropic"
    return MockClient(), "mock"


def build_stt(env: dict[str, str] | None = None) -> tuple[STTEngine, str]:
    """mock | whisper | deepgram | elevenlabs | route — режим и ключи из env."""
    env = env if env is not None else os.environ
    mode = env.get("STT_MODE", "mock")
    kwargs: dict[str, Any] = {"model_size": env.get("WHISPER_MODEL", "large-v3")}
    if mode == "deepgram":
        kwargs.update(api_key=env["DEEPGRAM_API_KEY"],
                      model=env.get("DEEPGRAM_MODEL", "nova-3"),
                      language=env.get("DEEPGRAM_LANG", "multi"))
    elif mode == "elevenlabs":
        kwargs.update(api_key=env["ELEVENLABS_API_KEY"])
    elif mode == "route":
        kwargs.update(deepgram_key=env["DEEPGRAM_API_KEY"],
                      deepgram_model=env.get("DEEPGRAM_MODEL", "nova-3"),
                      deepgram_language=env.get("DEEPGRAM_LANG", "multi"),
                      elevenlabs_key=env["ELEVENLABS_API_KEY"])
    return _stt_engine(mode, **kwargs), mode
