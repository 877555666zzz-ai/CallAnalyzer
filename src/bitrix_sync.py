"""
Синхронизация сделок из Bitrix24 в таблицу Deal (для конверсий, скорости тёплых, сверки с CRM).
Использует crm.deal.list с пагинацией. Маппинг полей — под вашу воронку (см. STAGE/UF ниже).
"""
from __future__ import annotations
from datetime import datetime
from typing import Any

from .bitrix_client import BitrixClient
from .db import Deal

# Воронка «Продажи» и стадия «Теплые лиды» (получено разведкой crm.status.list).
WARM_CATEGORY_ID = 0
# в истории стадий id категории-0 приходит как 'C0:NEW' либо просто 'NEW' — принимаем оба
WARM_STAGE_IDS = {"NEW", "C0:NEW"}

# Поля карточки Bitrix. UF_* — кастомные поля под вашу воронку; подставьте свои коды.
SELECT = ["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "CONTACT_ID", "COMPANY_ID",
          "DATE_CREATE", "CLOSED", "UF_CRM_IS_WARM", "UF_CRM_WARM_AT", "UF_CRM_FIRST_CALL_AT"]


def _digits(s: Any) -> str:
    """Оставляет только цифры телефона (для сравнения номеров в разных форматах)."""
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def warm_phones(bitrix: BitrixClient) -> set[str]:
    """
    Телефоны сделок, которые СЕЙЧАС на стадии «Теплые лиды» (воронка Продажи).
    У этой CRM номер лежит в названии сделки (TITLE) и в телефоне компании — берём оба.
    Возвращает множество номеров (последние 10 цифр) для матчинга со звонками.
    """
    phones: set[str] = set()
    start = 0
    flt = {"CATEGORY_ID": WARM_CATEGORY_ID, "STAGE_ID": "NEW"}
    while True:
        resp = bitrix.call("crm.deal.list", {
            "select": ["ID", "TITLE", "COMPANY_ID", "STAGE_ID"],
            "filter": flt,
            "order": {"ID": "ASC"},
            "start": start,
        })
        rows = resp or []
        if not rows:
            break
        for r in rows:
            # 1) телефон прямо в названии сделки
            d = _digits(r.get("TITLE"))
            if len(d) >= 10:
                phones.add(d[-10:])
            # 2) подстраховка — телефон компании
            comp_id = r.get("COMPANY_ID")
            if comp_id and str(comp_id) != "0":
                try:
                    comp = bitrix.call("crm.company.get", {"id": comp_id}) or {}
                    for p in (comp.get("PHONE") or []):
                        v = p.get("VALUE") if isinstance(p, dict) else p
                        dv = _digits(v)
                        if len(dv) >= 10:
                            phones.add(dv[-10:])
                except Exception:
                    pass
        if len(rows) < 50:
            break
        start += 50
    return phones


def _deal_phones(bitrix: BitrixClient, deal_id: Any) -> list[str]:
    """Оставлено для совместимости — телефоны через контакты (в этой CRM обычно пусто)."""
    out: list[str] = []
    try:
        contacts = bitrix.call("crm.deal.contact.items.get", {"id": deal_id}) or []
        for c in contacts:
            cid = c.get("CONTACT_ID") or c.get("id")
            if not cid:
                continue
            info = bitrix.call("crm.contact.get", {"id": cid}) or {}
            for p in (info.get("PHONE") or []):
                v = p.get("VALUE") if isinstance(p, dict) else p
                if v:
                    out.append(v)
    except Exception:
        pass
    return out


def warm_deal_ids(bitrix: BitrixClient, date_from: datetime | None = None,
                  progress: bool = False, max_pages: int = 200) -> set[str]:
    """
    ID сделок, которые СЕЙЧАС на стадии «Теплые лиды» (простой фильтр по текущей стадии).
    """
    ids: set[str] = set()
    start = 0
    flt: dict[str, Any] = {"CATEGORY_ID": WARM_CATEGORY_ID, "STAGE_ID": "NEW"}
    for _ in range(max_pages):
        resp = bitrix.call("crm.deal.list", {
            "select": ["ID", "STAGE_ID"],
            "filter": flt,
            "order": {"ID": "ASC"},
            "start": start,
        })
        rows = resp or []
        if not rows:
            break
        for r in rows:
            ids.add(str(r.get("ID")))
        if progress:
            print(f"  ...тёплых сейчас: {len(ids)}", flush=True)
        if len(rows) < 50:
            break
        start += 50
    return ids


def warm_deal_ids_strict(bitrix: BitrixClient, date_from: datetime | None = None,
                         progress: bool = False, max_pages: int = 400) -> set[str]:
    """
    Точный вариант «был именно на стадии Теплые лиды» через историю стадий.
    Медленнее (листает crm.stagehistory.list). Использовать, если нужна строгость.
    """
    ids: set[str] = set()
    start = 0
    flt: dict[str, Any] = {"CATEGORY_ID": WARM_CATEGORY_ID}
    if date_from:
        flt[">=CREATED_TIME"] = date_from.strftime("%Y-%m-%dT%H:%M:%S")

    for page in range(max_pages):
        resp = bitrix.call("crm.stagehistory.list", {
            "entityTypeId": 2,
            "filter": flt,
            "select": ["OWNER_ID", "STAGE_ID"],
            "order": {"CREATED_TIME": "ASC"},
            "start": start,
        })
        rows = resp.get("items", resp) if isinstance(resp, dict) else resp
        rows = rows or []
        if not rows:
            break
        for r in rows:
            stage = str(r.get("STAGE_ID", ""))
            if stage in WARM_STAGE_IDS or (stage.startswith("C0") and stage.endswith(":NEW")):
                oid = str(r.get("OWNER_ID") or "")
                if oid:
                    ids.add(oid)
        if progress:
            print(f"  ...страница {page+1}, тёплых собрано: {len(ids)}", flush=True)
        if len(rows) < 50:
            break
        start += 50
    return ids


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