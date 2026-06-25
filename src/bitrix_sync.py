"""
Синхронизация сделок из Bitrix24 в таблицу Deal (для конверсий, скорости тёплых, сверки с CRM).
Использует crm.deal.list с пагинацией. Маппинг полей — под вашу воронку (см. STAGE/UF ниже).
"""
from __future__ import annotations
from datetime import datetime
from typing import Any

from .bitrix_client import BitrixClient
from .db import Deal

# Поля карточки Bitrix. UF_* — кастомные поля под вашу воронку; подставьте свои коды.
SELECT = ["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "CONTACT_ID", "COMPANY_ID",
          "DATE_CREATE", "CLOSED", "UF_CRM_IS_WARM", "UF_CRM_WARM_AT", "UF_CRM_FIRST_CALL_AT"]


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def sync_deals(bitrix: BitrixClient, session, date_from: datetime | None = None) -> int:
    start = 0
    count = 0
    flt: dict[str, Any] = {}
    if date_from:
        flt[">=DATE_CREATE"] = date_from.strftime("%Y-%m-%dT%H:%M:%S")

    while True:
        resp = bitrix.call("crm.deal.list", {"select": SELECT, "filter": flt,
                                             "order": {"ID": "ASC"}, "start": start})
        items = resp or []
        if not items:
            break
        for it in items:
            session.merge(Deal(
                id=str(it["ID"]),
                client_number=str(it.get("PHONE") or it.get("CONTACT_ID") or ""),  # телефон тянется отдельно при необходимости
                stage=it.get("STAGE_ID"),
                amount=float(it.get("OPPORTUNITY") or 0),
                won=(str(it.get("STAGE_ID", "")).upper().endswith("WON")),
                is_warm=str(it.get("UF_CRM_IS_WARM", "")) in ("1", "Y", "true", "True"),
                warm_at=_dt(it.get("UF_CRM_WARM_AT")),
                first_call_at=_dt(it.get("UF_CRM_FIRST_CALL_AT")),
                created_at=_dt(it.get("DATE_CREATE")),
            ))
            count += 1
        session.commit()
        if len(items) < 50:   # Bitrix отдаёт по 50 на страницу
            break
        start += 50
    return count
