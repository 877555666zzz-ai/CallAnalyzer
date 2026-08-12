"""
Топы для прослушивания (§7): пресеты «самые проблемные / успешные / конфликтные / аномальные».
Произвольная фильтрация — поверх этого в дашборде.
"""
from __future__ import annotations
from typing import Any

from .db import Call, Analysis, Manager


def _rows(session, project: str | None):
    q = session.query(Call, Analysis, Manager).join(Analysis, Analysis.call_id == Call.id)\
        .outerjoin(Manager, Manager.id == Call.manager_id)
    if project:
        q = q.filter(Call.project == project)
    out = []
    for call, an, mgr in q.all():
        a = an.data
        passed = sum(1 for c in a["checklist"] if c.get("passed") is True)
        total = sum(1 for c in a["checklist"] if c.get("passed") is not None)
        score = round(100 * passed / total) if total else 0
        high_flags = sum(1 for rf in a["redflags"] if rf["severity"] == "high")
        client_flags = sum(1 for rf in a["redflags"] if rf["who"] == "client")
        out.append({
            "id": call.id, "time": call.started_at, "manager": mgr.full_name if mgr else "—",
            "client": call.client_number, "result": a["result_classification"]["primary"],
            "score": score, "redflags": len(a["redflags"]), "high_flags": high_flags,
            "client_flags": client_flags, "talk": a["metrics"]["talk_ratio_operator_pct"],
            "duration": a["metrics"]["total_duration_sec"], "summary": a["summary"],
        })
    return out


def tops(session, project: str | None = None, n: int = 20) -> dict[str, list[dict[str, Any]]]:
    rows = _rows(session, project)
    if not rows:
        return {"problematic": [], "success": [], "conflict": [], "anomaly_long": [], "anomaly_short": []}

    # «Проблемный» — не просто N худших по счёту из того что есть (на маленькой выборке
    # [:n] не фильтрует вообще ничего, и туда попадают даже success-звонки), а звонок с
    # реальным сигналом проблемы: низкий скрипт-скор или высокий ред-флаг, и не успех.
    problematic = sorted(
        [r for r in rows if r["result"] != "success" and (r["score"] < 70 or r["high_flags"] > 0)],
        key=lambda r: (r["score"], -r["high_flags"]),
    )
    success = [r for r in rows if r["result"] == "success"]
    conflict = sorted([r for r in rows if r["client_flags"] or r["result"] == "aggressive"],
                      key=lambda r: -r["client_flags"])

    # «Аномально длинный» — статистически нетипичный относительно СВОЕЙ выборки, а не
    # «самый длинный из того что есть» (иначе на 7 демо-звонках туда попадают все 7,
    # и «длинные»/«короткие» превращаются в один и тот же список в разном порядке).
    avg_duration = sum(r["duration"] for r in rows) / len(rows)
    long_threshold = max(avg_duration * 1.8, 120.0)
    anomaly_long = sorted([r for r in rows if r["duration"] > long_threshold], key=lambda r: -r["duration"])
    anomaly_short = sorted([r for r in rows if r["duration"] < 45], key=lambda r: r["duration"])

    return {
        "problematic": problematic[:n],
        "success": success[:n],
        "conflict": conflict[:n],
        "anomaly_long": anomaly_long[:n],
        "anomaly_short": anomaly_short[:n],
    }
