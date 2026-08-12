"""
Запись звонка и разбора в БД + резолв маппинга менеджера (§4.2).
Ключ привязки провайдер-нейтральный: сначала пробуем kcell_login (Kcell), потом
internal_number (легаси Sipuni). Если ни один не найден — звонок уходит в
mapping_unmatched (ничего не теряем).
"""
from __future__ import annotations
from datetime import datetime
from typing import Any

from .db import Manager, Call, Analysis, Transcript, UnmatchedCall


def upsert_manager(session, full_name: str, department: str, project: str,
                   internal_number: str | None = None, kcell_login: str | None = None) -> Manager:
    assert internal_number or kcell_login, "нужен хотя бы один ключ привязки"
    q = session.query(Manager)
    m = None
    if kcell_login:
        m = q.filter_by(kcell_login=kcell_login).one_or_none()
    if m is None and internal_number:
        m = q.filter_by(internal_number=internal_number).one_or_none()
    if m is None:
        m = Manager(full_name=full_name, internal_number=internal_number, kcell_login=kcell_login,
                    department=department, project=project)
        session.add(m)
        session.flush()
    else:
        # обновляем недостающий ключ, если менеджер уже был заведён по-другому провайдеру
        if kcell_login and not m.kcell_login:
            m.kcell_login = kcell_login
        if internal_number and not m.internal_number:
            m.internal_number = internal_number
    return m


def find_manager(session, key: str | None):
    """key — operator_login (Kcell) или operator_internal_number (Sipuni), что есть у звонка."""
    if not key:
        return None
    m = session.query(Manager).filter_by(kcell_login=key).one_or_none()
    if m:
        return m
    return session.query(Manager).filter_by(internal_number=key).one_or_none()


def managed_keys(session) -> set[str]:
    """Множество всех ключей привязки (kcell_login + internal_number) заведённых менеджеров —
    «зона» обработки. Ровно эти ключи пропускаются в анализ; всё остальное — вне зоны."""
    keys: set[str] = set()
    for login, internal in session.query(Manager.kcell_login, Manager.internal_number).all():
        if login:
            keys.add(login)
        if internal:
            keys.add(internal)
    return keys


def record_unmatched(session, call_id: str, key: str | None, started_at, reason: str) -> None:
    session.merge(UnmatchedCall(
        id=call_id, internal_number=key or "?",
        started_at=started_at, reason=reason,
    ))
    session.commit()


def save_call_with_analysis(session, call: dict[str, Any], analysis: dict[str, Any]) -> None:
    md = call.get("metadata", {})
    key = md.get("operator_login") or md.get("operator_internal_number")
    manager = find_manager(session, key)

    if manager is None:
        # §4.2 — несвязанный звонок, в отдельную таблицу для контроля
        session.merge(UnmatchedCall(
            id=call["call_id"], internal_number=key or "?",
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