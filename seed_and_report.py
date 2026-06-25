#!/usr/bin/env python3
"""
Демо спины системы: синтезируем несколько звонков по разным менеджерам,
прогоняем через РЕАЛЬНЫЙ пайплайн (analyzer → метрики → БД), затем строим отчёт «где деньги».
LLM — scripted-mock (заглушка под реальную модель); метрики считаются по-настоящему.

Запуск:  python seed_and_report.py
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from src.analyzer import analyze_call, load_config
from src.llm_client import MockClient
from src.db import get_engine, get_sessionmaker, Base, Deal, Recording
from src.store import upsert_manager, save_call_with_analysis, find_manager
from src.report_money import build_money_report, render_telegram
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parent
LABELS = {
    "introduced": "Представился и назвал компанию",
    "greeted": "Поздоровался",
    "empathy": "Эмпатия / фразы присоединения",
    "handled_objections": "Отработал возражения",
    "handled_price_objection": "Отработал возражение по цене",
    "proposed_next_step": "Предложил следующий шаг / закрытие / апсейл",
}


def checklist(passed: dict, scores: dict | None = None):
    scores = scores or {}
    return [{"id": k, "label": LABELS[k], "passed": passed.get(k),
             **({"score": scores[k]} if k in scores else {}), "evidence": ""} for k in LABELS]


def seg(speaker, start, end, text, lang="ru"):
    return {"speaker": speaker, "start": start, "end": end, "text": text, "lang": lang}

# короткий «болтливый» каркас потери по цене
LOSS_SEGS = [
    seg("operator", 0, 3.5, "Здравствуйте, меня зовут Оператор, компания Яндекс Такси."),
    seg("client", 4, 6, "Иә, тыңдап тұрмын.", "kk"),
    seg("operator", 6.2, 12, "Біз корпоративтік такси ұсынамыз. Очень выгодно для бизнеса."),
    seg("client", 12.5, 16, "А сколько стоит? Мне дорого, бизнес небольшой."),
    seg("operator", 20.5, 27, "Ну это зависит... зато подача машины до 5 минут."),
    seg("client", 27.5, 31, "Мне цена важна. Сколько в итоге?"),
    seg("operator", 31.2, 35, "Я на почту тарифы скину."),
    seg("client", 35.2, 37, "Хорошо, до свидания."),
]
SUCCESS_SEGS = [
    seg("operator", 0, 4, "Добрый день! Меня зовут Оператор, Яндекс Такси, корпоративный отдел."),
    seg("client", 4.3, 7, "Здравствуйте, слушаю."),
    seg("operator", 7.2, 11, "Понимаю, для бизнеса важна прозрачность по цене — давайте посчитаю под ваш объём."),
    seg("client", 11.5, 18, "Хорошо, у нас примерно 40 поездок в месяц по городу."),
    seg("operator", 18.3, 26, "Отлично, тогда вот тариф и условия. Оформим онлайн-оферту сейчас?"),
    seg("client", 26.5, 30, "Да, давайте оформлять."),
    seg("operator", 30.2, 33, "Отправляю ссылку, помогу заполнить."),
]
FIZIK_SEGS = [
    seg("operator", 0, 3.5, "Здравствуйте, Яндекс Такси, корпоративный отдел."),
    seg("client", 4, 8, "А я обычный человек, у меня нет никакой компании."),
    seg("operator", 8.3, 11, "Понял, извините за беспокойство, всего доброго."),
]
AGGR_SEGS = [
    seg("operator", 0, 3.5, "Здравствуйте, Яндекс Такси..."),
    seg("client", 4, 9, "Сколько можно звонить?! Отстаньте уже от меня!"),
    seg("operator", 9.3, 12, "Прошу прощения, снимаю номер из обзвона."),
]
DROP_SEGS = [
    seg("operator", 0, 4, "Здравствуйте, меня зовут Оператор, Яндекс Такси."),
    seg("client", 4.3, 6, "Да-да, говор..."),
]

CALLS = [
    # (call_id, internal, name, segments, scripted_analysis)
    ("c1", "234", "Айгерим", LOSS_SEGS, {
        "summary": "Возражение по цене не отработано, оператор ушёл в скорость подачи. Мягкий отказ.",
        "loss_stage": "after_price",
        "result_classification": {"primary": "not_relevant", "secondary": ["reached"], "confidence": 0.8},
        "refusal_reason": "Потеря после вопроса о цене — стоимость не названа, ценность не донесена.",
        "checklist": checklist({"introduced": True, "greeted": True, "empathy": False,
                                "handled_objections": False, "handled_price_objection": False,
                                "proposed_next_step": False}, {"empathy": 15}),
        "redflags": [{"rule_id": "forbidden_taxi_eta", "type": "compliance_token", "who": "operator",
                      "severity": "high", "quote": "подача машины до 5 минут",
                      "explanation": "«до 5 минут» вместо «от» — лид не засчитывается."}],
    }),
    ("c2", "234", "Айгерим", SUCCESS_SEGS, {
        "summary": "Оператор отработал цену через расчёт под объём, предложил оферту, клиент согласился.",
        "loss_stage": "none",
        "result_classification": {"primary": "success", "secondary": [], "confidence": 0.9},
        "refusal_reason": None,
        "checklist": checklist({"introduced": True, "greeted": True, "empathy": True,
                                "handled_objections": True, "handled_price_objection": True,
                                "proposed_next_step": True}, {"empathy": 80}),
        "redflags": [],
    }),
    ("c3", "234", "Айгерим", LOSS_SEGS, {
        "summary": "Короткий звонок, оператор не дожал возражение по цене, следующий шаг не назначен.",
        "loss_stage": "after_price",
        "result_classification": {"primary": "not_relevant", "secondary": ["reached"], "confidence": 0.7},
        "refusal_reason": "Не отработано возражение по цене.",
        "checklist": checklist({"introduced": True, "greeted": True, "empathy": False,
                                "handled_objections": False, "handled_price_objection": False,
                                "proposed_next_step": False}, {"empathy": 20}),
        "redflags": [{"rule_id": "forbidden_taxi_eta", "type": "compliance_token", "who": "operator",
                      "severity": "high", "quote": "подача до 5 минут", "explanation": "Нарушение регламента."}],
    }),
    ("c4", "235", "Данияр", FIZIK_SEGS, {
        "summary": "Дозвон на физлицо, нет компании. Не целевой контакт.",
        "loss_stage": "no_contact",
        "result_classification": {"primary": "individual_not_legal", "secondary": [], "confidence": 0.95},
        "refusal_reason": "Физлицо — не целевая аудитория (вопрос к базе).",
        "checklist": checklist({"introduced": True, "greeted": True, "empathy": None,
                                "handled_objections": None, "handled_price_objection": None,
                                "proposed_next_step": None}),
        "redflags": [],
    }),
    ("c5", "235", "Данияр", LOSS_SEGS, {
        "summary": "Оператор сослался на «дешевле, чем для физлиц», цену не обосновал. Отказ.",
        "loss_stage": "after_price",
        "result_classification": {"primary": "not_relevant", "secondary": ["reached"], "confidence": 0.75},
        "refusal_reason": "Возражение по цене не отработано, использована серая формулировка.",
        "checklist": checklist({"introduced": True, "greeted": True, "empathy": False,
                                "handled_objections": False, "handled_price_objection": False,
                                "proposed_next_step": False}, {"empathy": 25}),
        "redflags": [{"rule_id": "false_cheaper_claim", "type": "grey_wording", "who": "operator",
                      "severity": "medium", "quote": "у нас дешевле, чем для физлиц",
                      "explanation": "Непроверяемое утверждение о цене."}],
    }),
    ("c6", "236", "Жанна", SUCCESS_SEGS, {
        "summary": "Чистая отработка: эмпатия, расчёт, закрытие на оферту.",
        "loss_stage": "none",
        "result_classification": {"primary": "success", "secondary": [], "confidence": 0.92},
        "refusal_reason": None,
        "checklist": checklist({"introduced": True, "greeted": True, "empathy": True,
                                "handled_objections": True, "handled_price_objection": True,
                                "proposed_next_step": True}, {"empathy": 85}),
        "redflags": [],
    }),
    ("c7", "236", "Жанна", AGGR_SEGS, {
        "summary": "Агрессивный абонент, оператор корректно снял номер из обзвона.",
        "loss_stage": "other",
        "result_classification": {"primary": "aggressive", "secondary": [], "confidence": 0.88},
        "refusal_reason": "Агрессивный абонент — не вина менеджера.",
        "checklist": checklist({"introduced": True, "greeted": True, "empathy": None,
                                "handled_objections": None, "handled_price_objection": None,
                                "proposed_next_step": None}),
        "redflags": [],
    }),
]


def main():
    cfg = load_config(ROOT / "configs" / "yandex_taxi_corp.yaml")
    engine = get_engine("sqlite:///" + str(ROOT / "out" / "demo.db"))
    Base.metadata.drop_all(engine)
    Session = get_sessionmaker(engine)

    with Session() as s:
        for internal, name in {("234", "Айгерим"), ("235", "Данияр"), ("236", "Жанна")}:
            upsert_manager(s, name, internal, "sales_taxi", "yandex_taxi_corp")
        s.commit()

        for call_id, internal, name, segments, scripted in CALLS:
            call = {"call_id": call_id, "metadata": {
                "datetime": "2026-06-22T11:00:00+05:00", "direction": "outbound",
                "operator_internal_number": internal, "operator_name": name,
                "department": "sales_taxi", "project": "yandex_taxi_corp",
                "client_number": f"+7700{call_id}", "channel": "stereo",
            }, "segments": segments}
            analysis = analyze_call(call, cfg, MockClient(scripted=scripted))
            save_call_with_analysis(s, call, analysis)

        # --- демо-сделки (для конверсий, скорости тёплых, сверки с CRM) ---
        base = datetime(2026, 6, 22, 9, 0, 0)
        mgr_by_internal = {internal: find_manager(s, internal).id for internal in ("234", "235", "236")}
        # (call_id, internal, stage, is_warm, is_legal, won, warm_min_before_call, amount)
        DEALS = [
            ("c1", "234", "КП",  True,  True,  False, 90,  120000),
            ("c2", "234", "WON", True,  True,  True,  15,  120000),
            ("c3", "234", "КП",  True,  True,  False, 180, 120000),
            ("c4", "235", "КП",  False, False, False, None, 0),       # физик в продажной стадии → сверка
            ("c5", "235", "КДЗ", True,  True,  False, 150, 120000),
            ("c6", "236", "WON", True,  True,  True,  10,  120000),
            ("c7", "236", "NEW", False, True,  False, None, 0),
        ]
        for cid, internal, stage, warm, legal, won, mins, amount in DEALS:
            warm_at = base if mins is not None else None
            first_call = (base + timedelta(minutes=mins)) if mins is not None else None
            s.merge(Deal(id=f"D{cid}", client_number=f"+7700{cid}", manager_id=mgr_by_internal[internal],
                         department="sales_taxi", project="yandex_taxi_corp", stage=stage,
                         is_warm=warm, is_legal=legal, won=won, amount=amount,
                         warm_at=warm_at, first_call_at=first_call, created_at=base))
            s.merge(Recording(id=f"{cid}-original", call_id=cid, kind="original",
                              object_path=f"out/audio/{cid}.mp3", immutable=True))
        s.commit()

        report = build_money_report(s, cfg["economics"])

    out = ROOT / "out" / "money_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render_telegram(report))
    print("\n--- JSON (для дашборда) сохранён:", out, "---")


if __name__ == "__main__":
    main()
