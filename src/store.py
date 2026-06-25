"""
Запись звонка и разбора в БД + резолв маппинга менеджера (§4.2).
Если внутренний номер Сипуни не найден — звонок уходит в mapping_unmatched (ничего не теряем).
"""
from __future__ import annotations
from datetime import datetime
from typing import Any

from .db import Manager, Call, Analysis, Transcript, UnmatchedCall


def upsert_manager(session, full_name: str, internal_number: str, department: str, project: str) -> Manager:
    m = session.query(Manager).filter_by(sipuni_internal_number=internal_number).one_or_none()
    if m is None:
        m = Manager(full_name=full_name, sipuni_internal_number=internal_number,
                    department=department, project=project)
        session.add(m)
        session.flush()
    return m


def find_manager(session, internal_number: str):
    return session.query(Manager).filter_by(sipuni_internal_number=internal_number).one_or_none()


def record_unmatched(session, call_id: str, internal_number: str | None, started_at, reason: str) -> None:
    session.merge(UnmatchedCall(
        id=call_id, sipuni_internal_number=internal_number or "?",
        started_at=started_at, reason=reason,
    ))
    session.commit()


def save_call_with_analysis(session, call: dict[str, Any], analysis: dict[str, Any]) -> None:
    md = call.get("metadata", {})
    internal = md.get("operator_internal_number")
    manager = session.query(Manager).filter_by(sipuni_internal_number=internal).one_or_none()

    if manager is None:
        # §4.2 — несвязанный звонок, в отдельную таблицу для контроля
        session.merge(UnmatchedCall(
            id=call["call_id"], sipuni_internal_number=internal or "?",
            started_at=datetime.fromisoformat(md["datetime"]),
            reason="internal_number_not_mapped",
        ))
        session.commit()
        return

    c = Call(
        id=call["call_id"],
        started_at=datetime.fromisoformat(md["datetime"]),
        direction=md.get("direction", "?"),
        duration_sec=analysis["metrics"]["total_duration_sec"],
        manager_id=manager.id,
        department=manager.department,
        project=manager.project,
        client_number=md.get("client_number", "?"),
        audio_url=md.get("audio_url"),
        channel=md.get("channel", "mono"),
        status="analyzed",
    )
    session.merge(c)
    session.merge(Analysis(call_id=call["call_id"], data=analysis))
    session.merge(Transcript(call_id=call["call_id"], segments=call.get("segments", [])))
    session.commit()
