"""
Оркестратор разбора звонка (раздел 6).
Качественные поля (чеклист, ред-флаги, классификация, причина отказа, сводка) — у LLM.
Метрики — детерминированно в коде. Финальный объект валидируется по JSON Schema.
"""
from __future__ import annotations
import json
import copy
import logging
from pathlib import Path
from typing import Any

import yaml
from jsonschema import validate

from .metrics import compute_metrics
from .llm_client import BaseLLMClient

log = logging.getLogger("analyzer")
ROOT = Path(__file__).resolve().parent.parent
FULL_SCHEMA = json.loads((ROOT / "analysis_schema.json").read_text(encoding="utf-8"))


def _llm_tool_schema() -> dict[str, Any]:
    """Схема для LLM = полная схема БЕЗ metrics (их считаем сами) и с ПЛОСКОЙ
    классификацией результата. Вложенный объект result_classification отдаём модели
    тремя скалярными полями: именно на вложенном объекте claude-opus-4-8 устойчиво
    ломает структуру tool_use (просачивается <parameter ...>-разметка). В analyze_call
    плоские поля детерминированно собираются обратно в result_classification."""
    s = copy.deepcopy(FULL_SCHEMA)
    s["properties"].pop("metrics", None)
    rc_props = s["properties"].pop("result_classification")["properties"]
    s["properties"]["result_primary"] = rc_props["primary"]
    s["properties"]["result_secondary"] = rc_props["secondary"]
    s["properties"]["result_confidence"] = rc_props["confidence"]
    s["required"] = [r for r in s["required"] if r not in ("metrics", "result_classification")]
    s["required"] += ["result_primary", "result_secondary", "result_confidence"]
    # summary — последним полем: на границе summary->следующее поле модель устойчиво
    # просачивает <parameter ...>-разметку и проглатывает соседнее поле. Если после
    # длинного free-text summary полей нет, просачивать некуда (перед ним идёт скаляр
    # result_confidence — число закрывается чисто и не «впитывает» разметку).
    summary = s["properties"].pop("summary")
    s["properties"]["summary"] = summary
    s["required"] = [r for r in s["required"] if r != "summary"] + ["summary"]
    return s


def load_config(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _render_transcript(segments: list[dict[str, Any]]) -> str:
    lines = []
    roles = {"operator": "ОПЕРАТОР", "client": "КЛИЕНТ"}
    for s in segments:
        role = roles.get(s["speaker"], "РЕПЛИКА")
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
        f"3) result_primary — одна из категорий: {categories}. result_secondary — доп. категории (массив, можно пустой), result_confidence — уверенность 0..1.\n"
        "3a) loss_stage — на каком этапе потеряли лид: none|after_price|at_contract|at_transfer|call_dropped|no_contact|other.\n"
        "4) refusal_reason — одна фраза, ПОЧЕМУ сделка не закрылась (для карточки CRM). null если успех.\n"
        "5) summary — 2-4 предложения: суть, ключевые возражения, итог.\n"
        "НЕ считай числовые метрики (talk ratio, паузы) — это делает система отдельно."
    )

    md = call.get("metadata", {})
    roles_unknown = all(s.get("speaker") not in ("operator", "client") for s in call["segments"]) and bool(call["segments"])
    note = ("\n\nВНИМАНИЕ: роли в транскрипте НЕ размечены (моно-запись). Определи по смыслу, "
            "где говорит ОПЕРАТОР, а где КЛИЕНТ, и веди разбор соответственно." if roles_unknown else "")
    user = (
        f"МЕТАДАННЫЕ: направление={md.get('direction')}, оператор={md.get('operator_name')} "
        f"(вн.{md.get('operator_internal_number')}), проект={md.get('project')}.\n\n"
        f"ТРАНСКРИПТ:\n{_render_transcript(call['segments'])}{note}"
    )
    return system, user


def analyze_call(call: dict[str, Any], cfg: dict[str, Any], client: BaseLLMClient,
                 attempts: int = 3) -> dict[str, Any]:
    system, user = build_prompts(call, cfg)
    last_err: Exception | None = None
    n = max(1, attempts)
    for attempt in range(n):
        try:
            qualitative = client.analyze(system, user, _llm_tool_schema())
            analysis = dict(qualitative)
            # плоские поля классификации -> вложенный result_classification (как ждёт FULL_SCHEMA).
            # MockClient уже отдаёт вложенный объект — тогда плоских полей нет, не трогаем.
            if "result_primary" in analysis:
                analysis["result_classification"] = {
                    "primary": analysis.pop("result_primary"),
                    "secondary": analysis.pop("result_secondary", []) or [],
                    "confidence": analysis.pop("result_confidence", None),
                }
            analysis["metrics"] = compute_metrics(call["segments"], cfg.get("metrics"))
            validate(instance=analysis, schema=FULL_SCHEMA)  # бросит исключение, если LLM вернул мусор
            return analysis
        except Exception as e:  # стохастический сбой структуры — пробуем ещё раз
            last_err = e
            log.warning("analyze_call %s: попытка %d/%d не удалась: %s",
                        call.get("call_id", "?"), attempt + 1, n, e)
    raise last_err