"""
Отчёты Этапа 2 (§7): конверсии в разрезах, скорость отработки тёплого лида, сверка с CRM.
Источник — таблица Deal (синхронизируется из Bitrix) + Analysis (наш разбор).
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from typing import Any

from .db import Deal, Analysis, Call, Manager

# стадии, считающиеся «успехом» / «проигрышем» — настраивается под воронку заказчика
WON_STAGES = {"WON", "C1:WON", "ЗАКРЫТА", "SUCCESS"}
LOST_STAGES = {"LOSE", "C1:LOSE", "ПРОВАЛЕНА", "FAIL"}
KP_KDZ_STAGES = {"КП", "КДЗ", "KP", "KDZ", "PREPAYMENT_INVOICE", "EXECUTING"}


def _won(d: Deal) -> bool:
    if d.won is not None:
        return d.won
    return (d.stage or "").upper() in WON_STAGES


def conversions(session, project: str | None = None) -> dict[str, Any]:
    q = session.query(Deal)
    if project:
        q = q.filter(Deal.project == project)
    deals = q.all()

    warm = [d for d in deals if d.is_warm]
    warm_won = [d for d in warm if _won(d)]
    kp = [d for d in deals if (d.stage or "").upper() in KP_KDZ_STAGES or _won(d)]
    kp_won = [d for d in kp if _won(d)]
    legal = [d for d in deals if d.is_legal is True]
    fiz = [d for d in deals if d.is_legal is False]

    def rate(num, den):
        return round(100 * num / den, 1) if den else 0.0

    return {
        "warm_total": len(warm),
        "warm_to_won": len(warm_won),
        "warm_to_won_pct": rate(len(warm_won), len(warm)),
        "warm_to_lost": len(warm) - len(warm_won),
        "kp_kdz_total": len(kp),
        "kp_kdz_to_won_pct": rate(len(kp_won), len(kp)),
        "legal_conv_pct": rate(sum(1 for d in legal if _won(d)), len(legal)),
        "individual_conv_pct": rate(sum(1 for d in fiz if _won(d)), len(fiz)),
        "legal_total": len(legal),
        "individual_total": len(fiz),
    }


def warm_lead_speed(session, project: str | None = None) -> dict[str, Any]:
    """Время от попадания в «тёплые» до первого звонка менеджера. Чем быстрее — тем выше конверсия (§7)."""
    q = session.query(Deal, Manager).outerjoin(Manager, Manager.id == Deal.manager_id)
    if project:
        q = q.filter(Deal.project == project)

    buckets = {"<30мин": 0, "30–120мин": 0, ">120мин": 0}
    per_mgr: dict[str, list[float]] = defaultdict(list)
    won_by_bucket = {"<30мин": [0, 0], "30–120мин": [0, 0], ">120мин": [0, 0]}  # [won, total]

    for d, mgr in q.all():
        if not (d.warm_at and d.first_call_at):
            continue
        minutes = (d.first_call_at - d.warm_at).total_seconds() / 60.0
        b = "<30мин" if minutes <= 30 else ("30–120мин" if minutes <= 120 else ">120мин")
        buckets[b] += 1
        won_by_bucket[b][1] += 1
        if _won(d):
            won_by_bucket[b][0] += 1
        if mgr:
            per_mgr[mgr.full_name].append(minutes)

    managers = [{"manager": name, "avg_minutes": round(sum(v) / len(v), 1), "leads": len(v)}
                for name, v in sorted(per_mgr.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))]
    conv_by_bucket = {b: (round(100 * w / t, 1) if t else 0.0) for b, (w, t) in won_by_bucket.items()}
    return {"buckets": buckets, "conversion_by_bucket": conv_by_bucket, "managers": managers}


def crm_reconciliation(session, project: str | None = None) -> dict[str, Any]:
    """
    Сверка классификации системы с тем, что менеджер проставил в Bitrix (§7, «зона ответственности»).
    Кейс: система видит физика, а менеджер закинул сделку в КП/КДЗ → вопрос к менеджеру.
    Если и система, и CRM согласны (физик) → вопрос к базе.
    """
    deals = session.query(Deal).filter(Deal.project == project).all() if project \
        else session.query(Deal).all()

    result = {"checked": 0, "agree": 0, "disagree": 0,
              "manager_fault": [], "base_fault": [], "by_manager": defaultdict(lambda: {"agree": 0, "disagree": 0})}

    for d in deals:
        # берём последний разбор по этому номеру
        row = session.query(Analysis, Call, Manager).join(Call, Call.id == Analysis.call_id)\
            .outerjoin(Manager, Manager.id == Call.manager_id)\
            .filter(Call.client_number == d.client_number)\
            .order_by(Call.started_at.desc()).first()
        if not row:
            continue
        analysis, call, mgr = row
        sys_is_individual = analysis.data["result_classification"]["primary"] == "individual_not_legal"
        result["checked"] += 1
        mgr_name = mgr.full_name if mgr else "—"

        # система говорит «физик»
        if sys_is_individual:
            in_sales_stage = (d.stage or "").upper() in KP_KDZ_STAGES or _won(d)
            if in_sales_stage:
                # менеджер протащил физика в продажную стадию → его зона
                result["disagree"] += 1
                result["by_manager"][mgr_name]["disagree"] += 1
                result["manager_fault"].append({"deal": d.id, "client": d.client_number,
                                                 "manager": mgr_name, "stage": d.stage})
            else:
                # и система, и CRM согласны, что физик → вопрос к базе
                result["agree"] += 1
                result["by_manager"][mgr_name]["agree"] += 1
                result["base_fault"].append({"deal": d.id, "client": d.client_number})
        else:
            result["agree"] += 1
            result["by_manager"][mgr_name]["agree"] += 1

    result["by_manager"] = {k: v for k, v in result["by_manager"].items()}
    return result


def build_stage2(session, project: str | None = None) -> dict[str, Any]:
    return {
        "conversions": conversions(session, project),
        "warm_speed": warm_lead_speed(session, project),
        "reconciliation": crm_reconciliation(session, project),
    }
