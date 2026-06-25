"""
Оркестратор разбора звонка (раздел 6).
Качественные поля (чеклист, ред-флаги, классификация, причина отказа, сводка) — у LLM.
Метрики — детерминированно в коде. Финальный объект валидируется по JSON Schema.
"""
from __future__ import annotations
import json
import copy
from pathlib import Path
from typing import Any

import yaml
from jsonschema import validate

from .metrics import compute_metrics
from .llm_client import BaseLLMClient

ROOT = Path(__file__).resolve().parent.parent
FULL_SCHEMA = json.loads((ROOT / "analysis_schema.json").read_text(encoding="utf-8"))


def _llm_tool_schema() -> dict[str, Any]:
    """Схема для LLM = полная схема БЕЗ metrics (их мы считаем сами)."""
    s = copy.deepcopy(FULL_SCHEMA)
    s["properties"].pop("metrics", None)
    s["required"] = [r for r in s["required"] if r != "metrics"]
    return s


def load_config(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _render_transcript(segments: list[dict[str, Any]]) -> str:
    lines = []
    for s in segments:
        role = "ОПЕРАТОР" if s["speaker"] == "operator" else "КЛИЕНТ"
        lines.append(f"[{s['start']:.1f}-{s['end']:.1f}] {role} ({s.get('lang','?')}): {s['text']}")
    return "\n".join(lines)


def build_prompts(call: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str]:
    checklist_desc = "\n".join(
        f"  - {c['id']}: {c['label']}"
        + (" [ИЗМЕРИМЫЙ 0-100]" if c.get("measure") == "percent" else "")
        + (" [КРИТИЧНЫЙ]" if c.get("critical") else "")
        for c in cfg["checklist"]
    )
    redflag_desc = "\n".join(
        f"  - {r['id']} (type={r['type']}, who={r['who']}, severity={r['severity']}): {r.get('description','')}"
        + (f" Маркеры-подсказки: {r['patterns']}" if r.get("patterns") else "")
        for r in cfg["redflag_rules"]
    )
    categories = ", ".join(cfg["result_categories"])

    system = (
        "Ты — старший аналитик звонков колл-центра (РУ/КЗ, продажи корпоративного такси). "
        "На вход — транскрипт с ролями (ОПЕРАТОР/КЛИЕНТ), таймкодами и языком сегмента; в звонке встречается "
        "переключение языков (русский/казахский) — анализируй смысл целиком. "
        "Верни строго структурированный разбор через инструмент emit_call_analysis. Без воды, формулировки деловые, на русском.\n\n"
        "ПРАВИЛА РАЗБОРА:\n"
        "1) checklist — оцени КАЖДЫЙ пункт ниже (passed: true/false/null; для измеримых дай score 0-100; "
        "evidence — короткая цитата). Скрипт — это скелет, важно соблюдение сути, а не дословность.\n"
        f"{checklist_desc}\n"
        "2) redflags — фиксируй нарушения по правилам ниже. Указывай who (operator|client) — КТО нарушил. "
        "patterns это подсказки, но суди по смыслу (формулировки бывают разными). quote — дословный фрагмент.\n"
        f"{redflag_desc}\n"
        f"3) result_classification.primary — одна из категорий: {categories}.\n"
        "3a) loss_stage — на каком этапе потеряли лид: none|after_price|at_contract|at_transfer|call_dropped|no_contact|other.\n"
        "4) refusal_reason — одна фраза, ПОЧЕМУ сделка не закрылась (для карточки CRM). null если успех.\n"
        "5) summary — 2-4 предложения: суть, ключевые возражения, итог.\n"
        "НЕ считай числовые метрики (talk ratio, паузы) — это делает система отдельно."
    )

    md = call.get("metadata", {})
    user = (
        f"МЕТАДАННЫЕ: направление={md.get('direction')}, оператор={md.get('operator_name')} "
        f"(вн.{md.get('operator_internal_number')}), проект={md.get('project')}.\n\n"
        f"ТРАНСКРИПТ:\n{_render_transcript(call['segments'])}"
    )
    return system, user


def analyze_call(call: dict[str, Any], cfg: dict[str, Any], client: BaseLLMClient) -> dict[str, Any]:
    system, user = build_prompts(call, cfg)
    qualitative = client.analyze(system, user, _llm_tool_schema())

    analysis = dict(qualitative)
    analysis["metrics"] = compute_metrics(call["segments"], cfg.get("metrics"))

    validate(instance=analysis, schema=FULL_SCHEMA)  # бросит исключение, если LLM вернул мусор
    return analysis
