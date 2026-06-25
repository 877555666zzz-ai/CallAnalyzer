"""
Отчёт «где деньги» (раздел 7 ТЗ) — главный ответ руководителю.
Считает по сохранённым разборам: где и сколько теряем, по чьей вине (база vs менеджер),
сколько прибыли реально было спасти (what-if), какие ред-флаги повторяются.

Все суждения качества берём из analysis.data (LLM), все деньги — из economics конфига.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from .db import Call, Analysis, Manager

# категории, где потеря НЕ на менеджере (база/обстоятельства) — для «зоны ответственности»
BASE_FAULT = {"individual_not_legal", "wrong_number", "no_contact", "not_reached", "aggressive"}
MANAGER_FAULT_STAGES = {"after_price", "at_contract", "at_transfer"}
IDEAL_TALK_RATIO = 46.0  # эмпирика заказчика: оператор ~46% в успешном звонке


def _is_recoverable(a: dict[str, Any]) -> bool:
    """Спасаемая потеря: вина похожа на менеджера (этап/недоработка), а не на базу."""
    if a["result_classification"]["primary"] == "success":
        return False
    if a["result_classification"]["primary"] in BASE_FAULT:
        return False
    if a.get("loss_stage") in MANAGER_FAULT_STAGES:
        return True
    by_id = {c["id"]: c for c in a["checklist"]}
    price = by_id.get("handled_price_objection") or by_id.get("handled_objections")
    nextstep = by_id.get("proposed_next_step")
    return bool((price and price.get("passed") is False) or (nextstep and nextstep.get("passed") is False))


def build_money_report(session, economics: dict[str, Any],
                       date_from: datetime | None = None, date_to: datetime | None = None,
                       department: str | None = None, project: str | None = None) -> dict[str, Any]:
    avg_deal = float(economics.get("avg_deal_value", 0))
    recovery_rate = float(economics.get("recovery_rate", 0))
    currency = economics.get("currency", "")

    q = session.query(Call, Analysis, Manager).join(Analysis, Analysis.call_id == Call.id)\
        .join(Manager, Manager.id == Call.manager_id)
    if date_from:
        q = q.filter(Call.started_at >= date_from)
    if date_to:
        q = q.filter(Call.started_at <= date_to)
    if department:
        q = q.filter(Call.department == department)
    if project:
        q = q.filter(Call.project == project)
    rows = q.all()

    per_mgr: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "calls": 0, "success": 0, "lost": 0, "recoverable": 0, "base_fault": 0,
        "redflags": 0, "talk_ratio_sum": 0.0,
    })
    loss_by_stage: Counter = Counter()
    loss_by_reason: Counter = Counter()
    redflags_by_rule: Counter = Counter()
    redflags_by_mgr_high: Counter = Counter()
    total = {"calls": 0, "success": 0, "lost": 0, "recoverable": 0, "base_fault": 0}

    for call, an, mgr in rows:
        a = an.data
        prim = a["result_classification"]["primary"]
        m = per_mgr[mgr.full_name]
        m["calls"] += 1
        total["calls"] += 1
        m["talk_ratio_sum"] += a["metrics"]["talk_ratio_operator_pct"]

        if prim == "success":
            m["success"] += 1; total["success"] += 1
        elif prim in BASE_FAULT:
            m["base_fault"] += 1; total["base_fault"] += 1
            loss_by_reason[a.get("refusal_reason") or prim] += 1
            loss_by_stage[a.get("loss_stage", "other")] += 1
        else:
            m["lost"] += 1; total["lost"] += 1
            loss_by_reason[a.get("refusal_reason") or prim] += 1
            loss_by_stage[a.get("loss_stage", "other")] += 1
            if _is_recoverable(a):
                m["recoverable"] += 1; total["recoverable"] += 1

        for rf in a["redflags"]:
            m["redflags"] += 1
            redflags_by_rule[rf["rule_id"]] += 1
            if rf["severity"] == "high" and rf["who"] == "operator":
                redflags_by_mgr_high[mgr.full_name] += 1

    # деньги
    potential_lost_value = total["lost"] * avg_deal
    recoverable_value = round(total["recoverable"] * avg_deal * recovery_rate)

    managers = []
    for name, m in sorted(per_mgr.items(), key=lambda kv: kv[1]["recoverable"], reverse=True):
        conv = round(100 * m["success"] / m["calls"], 1) if m["calls"] else 0.0
        managers.append({
            "manager": name,
            "calls": m["calls"],
            "success": m["success"],
            "conversion_pct": conv,
            "lost_manager_fault": m["lost"],
            "recoverable_leads": m["recoverable"],
            "recoverable_value": round(m["recoverable"] * avg_deal * recovery_rate),
            "base_fault_leads": m["base_fault"],
            "redflags": m["redflags"],
            "avg_talk_ratio_operator": round(m["talk_ratio_sum"] / m["calls"], 1) if m["calls"] else 0.0,
        })

    return {
        "currency": currency,
        "assumptions": {"avg_deal_value": avg_deal, "recovery_rate": recovery_rate},
        "totals": {
            **total,
            "potential_lost_value": potential_lost_value,
            "recoverable_value": recoverable_value,
        },
        "loss_by_stage": dict(loss_by_stage.most_common()),
        "loss_by_reason_top": loss_by_reason.most_common(5),
        "redflags_by_rule": dict(redflags_by_rule.most_common()),
        "redflag_alerts_operators": dict(redflags_by_mgr_high.most_common()),
        "managers": managers,
    }


def render_telegram(report: dict[str, Any]) -> str:
    cur = report["currency"]
    t = report["totals"]
    lines = [
        "📊 *Отчёт «где деньги»*",
        f"Звонков: {t['calls']} | успех: {t['success']} | потери (менеджер): {t['lost']} | база/физики: {t['base_fault']}",
        f"💸 Упущено (валовая): {t['potential_lost_value']:,} {cur}".replace(",", " "),
        f"🎯 Из них реально спасти (what-if): *{t['recoverable_value']:,} {cur}*".replace(",", " "),
        "",
        "*Где теряем (этап):* " + ", ".join(f"{k}={v}" for k, v in report["loss_by_stage"].items()),
    ]
    if report["redflag_alerts_operators"]:
        lines.append("🚩 *Ред-флаги операторов:* " +
                     ", ".join(f"{k} ({v})" for k, v in report["redflag_alerts_operators"].items()))
    lines.append("")
    lines.append("*По менеджерам (спасаемая прибыль):*")
    for m in report["managers"]:
        flag = " ⚠️" if abs(m["avg_talk_ratio_operator"] - IDEAL_TALK_RATIO) > 12 else ""
        lines.append(
            f"• {m['manager']}: конв {m['conversion_pct']}% | "
            f"спасти {m['recoverable_value']:,} {cur} | talk {m['avg_talk_ratio_operator']}%{flag} | "
            f"флагов {m['redflags']}".replace(",", " ")
        )
    return "\n".join(lines)
