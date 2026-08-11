"""
Синхронизация сделок из Bitrix24 в таблицу Deal (для конверсий, скорости тёплых, сверки с CRM).
Использует crm.deal.list с пагинацией. Маппинг полей — под вашу воронку (см. STAGE/UF ниже).
"""
from __future__ import annotations
from datetime import datetime
from typing import Any

from .bitrix_client import BitrixClient
from .db import Deal, Analysis, Call

# Воронка «Продажи» и стадия «Теплые лиды» (подтверждено живым crm.category.list /
# crm.status.list на боевом портале: категория 0 = "Продажи", STATUS_ID=NEW = "Теплые лиды").
WARM_CATEGORY_ID = 0
# в истории стадий id категории-0 приходит как 'C0:NEW' либо просто 'NEW' — принимаем оба
WARM_STAGE_IDS = {"NEW", "C0:NEW"}

# Поля карточки Bitrix. UF_* — кастомные поля под вашу воронку; подставьте свои коды.
# ПРИМЕЧАНИЕ: на боевом портале у сделки нет отдельного поля "телефон" — номер лежит
# в TITLE (подтверждено: TITLE == "+7XXXXXXXXXX" для всех проверенных сделок), поэтому
# PHONE в SELECT не запрашиваем — телефон достаём в sync_deals() тем же способом, что
# и warm_phones() ниже (TITLE, с фоллбеком на телефон компании).
SELECT = ["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "CONTACT_ID", "COMPANY_ID",
          "CATEGORY_ID", "ASSIGNED_BY_ID",
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


def _deal_phone(bitrix: BitrixClient, it: dict[str, Any]) -> str:
    """Телефон клиента по сделке. На боевом портале TITLE == номер телефона
    (см. SELECT/комментарий выше) — это основной путь, без доп. запросов.
    Фоллбек — телефон компании сделки (та же эвристика, что в warm_phones)."""
    d = _digits(it.get("TITLE"))
    if len(d) >= 10:
        return d[-10:]
    comp_id = it.get("COMPANY_ID")
    if comp_id and str(comp_id) != "0":
        try:
            comp = bitrix.call("crm.company.get", {"id": comp_id}) or {}
            for p in (comp.get("PHONE") or []):
                v = p.get("VALUE") if isinstance(p, dict) else p
                dv = _digits(v)
                if len(dv) >= 10:
                    return dv[-10:]
        except Exception:
            pass
    return ""


def sync_deals(bitrix: BitrixClient, session, project: str, date_from: datetime | None = None,
               category_id: int = WARM_CATEGORY_ID) -> int:
    """
    Наполняет таблицу Deal из Bitrix для отчётов Этапа 2 (/conversions, /boss).

    project      — пишется во все синхронизированные сделки (в этой CRM один портал = один
                   проект, как и у Manager.project — см. setup_managers.py), а не выводится
                   из данных Bitrix.
    category_id  — воронка Bitrix (по умолчанию 0 = "Продажи", подтверждено crm.category.list).
                   Другие воронки портала (Узбекистан/Яндекс 360/QUANTA/Акцепт) сюда НЕ попадают,
                   иначе конверсии этого проекта смешаются с чужими бизнес-линиями.

    ИЗВЕСТНОЕ ОГРАНИЧЕНИЕ: Deal.manager_id не проставляется. Bitrix отдаёт только
    ASSIGNED_BY_ID (ID пользователя Bitrix), а связки "пользователь Bitrix -> внутренний
    номер Сипуни" в системе нет, и текущий вебхук (scope=crm) не может читать user.get,
    чтобы её найти. Отчёты, не завязанные на менеджера (conversions/kp_kdz/legal-физик),
    работают и так; per-manager разрез в warm_lead_speed() будет пустым, пока эта связка
    не появится (см. README).
    """
    start = 0
    count = 0
    flt: dict[str, Any] = {"CATEGORY_ID": category_id}
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
                client_number=_deal_phone(bitrix, it),
                project=project,
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

    _enrich_is_legal(session)
    return count


def _enrich_is_legal(session) -> None:
    """
    is_legal нельзя взять из Bitrix напрямую: на этом портале у КАЖДОЙ сделки (и физик,
    и юрлицо) автоматически создаётся COMPANY_ID с COMPANY_TYPE=CUSTOMER — признака
    юрлицо/физлицо в CRM нет (подтверждено живым crm.company.get). Источник истины —
    наш собственный разбор звонка: result_classification.primary == "individual_not_legal".
    Проставляем по последнему разбору для номера телефона сделки; если звонков ещё не было
    — оставляем None (не гадаем).
    """
    deals = session.query(Deal).filter(Deal.client_number != "").all()
    for d in deals:
        # суффикс, не равенство: Call.client_number хранит номер как отдаёт Сипуни
        # (с "+"/кодом страны), d.client_number — канонические последние 10 цифр.
        row = session.query(Analysis).join(Call, Call.id == Analysis.call_id) \
            .filter(Call.client_number.like(f"%{d.client_number}")) \
            .order_by(Call.started_at.desc()).first()
        if row is None:
            continue
        d.is_legal = row.data["result_classification"]["primary"] != "individual_not_legal"
    session.commit()