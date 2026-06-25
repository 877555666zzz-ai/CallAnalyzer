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
    """Боевой клиент. Использует tool_use, чтобы заставить модель вернуть валидный JSON по схеме."""

    def __init__(self, model: str = "claude-opus-4-8", api_key: str | None = None):
        from anthropic import Anthropic  # импорт здесь, чтобы Mock работал без установленного SDK
        self.model = model
        self.client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def analyze(self, system: str, user: str, tool_schema: dict[str, Any]) -> dict[str, Any]:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=system,
            tools=[{
                "name": "emit_call_analysis",
                "description": "Вернуть структурированный разбор звонка строго по схеме.",
                "input_schema": tool_schema,
            }],
            tool_choice={"type": "tool", "name": "emit_call_analysis"},
            messages=[{"role": "user", "content": user}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "emit_call_analysis":
                return block.input
        raise RuntimeError("LLM не вернул tool_use с разбором")


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
