"""
Слой LLM-провайдера. Принудительный structured output через tool_use.
Провайдер заменяемый: AnthropicClient для боевого режима, MockClient — без ключа/для тестов.
"""
from __future__ import annotations
import os
import json
from typing import Any


class BaseLLMClient:
    def analyze(self, system: str, user: str, tool_schema: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class AnthropicClient(BaseLLMClient):
    """Боевой клиент. Использует tool_use, чтобы заставить модель вернуть валидный JSON по схеме.

    Модель берётся из env LLM_MODEL (по умолчанию Haiku 4.5 — самый бюджетный вариант).
    Переключение на Sonnet/Opus — без правки кода: LLM_MODEL=claude-sonnet-4-6.
    Системный промпт и схема инструмента одинаковы для всех звонков проекта, поэтому
    помечены cache_control (prompt caching): повторный вход стоит ~10% от обычного —
    основная экономия на объёме. Транскрипт идёт в user-сообщении и не кэшируется.
    """

    def __init__(self, model: str | None = None, api_key: str | None = None):
        from anthropic import Anthropic  # импорт здесь, чтобы Mock работал без установленного SDK
        self.model = model or os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")
        self.client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def analyze(self, system: str, user: str, tool_schema: dict[str, Any]) -> dict[str, Any]:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            # cache_control кэширует префикс (инструменты + system) — идентичен для всех звонков.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[{
                "name": "emit_call_analysis",
                "description": "Вернуть структурированный разбор звонка строго по схеме.",
                "input_schema": tool_schema,
                "cache_control": {"type": "ephemeral"},
            }],
            tool_choice={"type": "tool", "name": "emit_call_analysis"},
            messages=[{"role": "user", "content": user}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "emit_call_analysis":
                return block.input
        raise RuntimeError("LLM не вернул tool_use с разбором")


class GrokClient(BaseLLMClient):
    """Клиент xAI Grok (OpenAI-совместимый API). Structured output через function calling.

    Модель из env GROK_MODEL (по умолчанию grok-4.1-fast — самый бюджетный).
    Ключ из env XAI_API_KEY. Требует пакет openai: pip install openai
    Тот же контракт, что у AnthropicClient — возвращает dict по схеме.
    """

    BASE_URL = "https://api.x.ai/v1"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        from openai import OpenAI  # импорт здесь, чтобы не требовать SDK без Grok
        self.model = model or os.environ.get("GROK_MODEL", "grok-4.1-fast")
        self.client = OpenAI(
            api_key=api_key or os.environ.get("XAI_API_KEY"),
            base_url=self.BASE_URL,
        )

    def analyze(self, system: str, user: str, tool_schema: dict[str, Any]) -> dict[str, Any]:
        resp = self.client.chat.completions.create(
            model=self.model,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=[{
                "type": "function",
                "function": {
                    "name": "emit_call_analysis",
                    "description": "Вернуть структурированный разбор звонка строго по схеме.",
                    "parameters": tool_schema,
                },
            }],
            tool_choice={"type": "function", "function": {"name": "emit_call_analysis"}},
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            return json.loads(msg.tool_calls[0].function.arguments)
        raise RuntimeError("Grok не вернул function call с разбором")


class GeminiClient(BaseLLMClient):
    """Клиент Google Gemini через OpenAI-совместимый эндпоинт. Structured output — function calling.

    Модель из env GEMINI_MODEL (по умолчанию gemini-2.5-flash — быстрый и на бесплатном тире).
    Ключ из env GEMINI_API_KEY (бесплатно на aistudio.google.com). Требует пакет openai.
    ВНИМАНИЕ: бесплатный тир Google может использовать данные для обучения — только для тестов,
    не для боевых звонков с ПДн клиентов.
    """

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        from openai import OpenAI
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self.client = OpenAI(
            api_key=api_key or os.environ.get("GEMINI_API_KEY"),
            base_url=self.BASE_URL,
        )

    def analyze(self, system: str, user: str, tool_schema: dict[str, Any]) -> dict[str, Any]:
        resp = self.client.chat.completions.create(
            model=self.model,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=[{
                "type": "function",
                "function": {
                    "name": "emit_call_analysis",
                    "description": "Вернуть структурированный разбор звонка строго по схеме.",
                    "parameters": tool_schema,
                },
            }],
            tool_choice={"type": "function", "function": {"name": "emit_call_analysis"}},
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            return json.loads(msg.tool_calls[0].function.arguments)
        raise RuntimeError("Gemini не вернул function call с разбором")


class MockClient(BaseLLMClient):
    """Оффлайн-заглушка. Если передан scripted — возвращает его (для сидинга разных звонков),
    иначе — разбор демо-звонка. Метрики в обоих случаях считаются кодом отдельно."""

    def __init__(self, scripted: dict[str, Any] | None = None):
        self.scripted = scripted

    def analyze(self, system: str, user: str, tool_schema: dict[str, Any]) -> dict[str, Any]:
        if self.scripted is not None:
            return self.scripted
        return {
            "summary": (
                "Исходящий звонок по корпоративному такси. Клиент сразу обозначил возражение по цене "
                "(«небольшой бизнес, дорого»), но оператор ушёл от вопроса о стоимости, переключив разговор на скорость подачи. "
                "Возражение по цене не отработано, ценность продукта не донесена, следующий шаг сведён к «скину тарифы на почту». Итог — мягкий отказ."
            ),
            "loss_stage": "after_price",
            "result_classification": {
                "primary": "not_relevant",
                "secondary": ["reached"],
                "confidence": 0.78,
            },
            "refusal_reason": "Потеря после вопроса о цене — оператор не назвал стоимость и не отработал возражение, ценность не донесена.",
            "checklist": [
                {"id": "introduced", "label": "Представился и назвал компанию", "passed": True, "evidence": "«Меня зовут Айгерим, компания Яндекс Такси»"},
                {"id": "greeted", "label": "Поздоровался", "passed": True, "evidence": "«Алло, здравствуйте!»"},
                {"id": "empathy", "label": "Эмпатия / фразы присоединения", "passed": False, "score": 15, "evidence": "На «мне дорого» нет присоединения, сразу уход в скорость"},
                {"id": "handled_objections", "label": "Отработал возражения", "passed": False, "evidence": "Возражение по цене проигнорировано дважды"},
                {"id": "handled_price_objection", "label": "Отработал возражение по цене", "passed": False, "evidence": "Цена так и не названа, ценность не аргументирована"},
                {"id": "proposed_next_step", "label": "Предложил следующий шаг / закрытие / апсейл", "passed": False, "evidence": "«Скину тарифы на почту» — пассивный шаг без договорённости"},
            ],
            "redflags": [
                {
                    "rule_id": "forbidden_taxi_eta",
                    "type": "compliance_token",
                    "who": "operator",
                    "severity": "high",
                    "quote": "подача машины до 5 минут",
                    "explanation": "Обещание «до 5 минут» вместо «от 5 минут» — заказчик (Яндекс) не засчитает лид. Грубое нарушение регламента.",
                }
            ],
        }