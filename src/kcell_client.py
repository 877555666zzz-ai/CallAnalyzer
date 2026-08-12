"""
Адаптер Kcell ВАТС (CRM API) — замена SipuniClient. Тот же контракт: export(), download_record(),
operators(), map_row(). Плюс healthcheck() — специфично для Kcell, полезно перед первым прогоном.

Документация: docs/rest_api.pdf («REST API ВАТС (CRM)», выдаётся в кабинете:
Настройки → Интеграция с CRM → Ваша CRM → «Перейти к полному описанию»).

Ключевые отличия от Sipuni:
  - авторизация: заголовок X-API-KEY, без MD5-подписи, без IP-вайтлиста;
  - оператор адресуется ЛОГИНОМ (поле user в истории), а не внутренним номером — внутр. номер
    (ext) есть отдельно в /users и напрямую в строке истории не приходит (см. map_row ниже
    и src/store.py::find_manager — там основной ключ поиска kcell_login, не internal_number);
  - запись разговора отдаётся ГОТОВОЙ ССЫЛКОЙ в самой строке истории (поле record) — отдельного
    метода «скачать по ID» в API нет, поэтому download_record() требует record_url;
  - пагинация /history/json НЕ подтверждена документацией — пример ответа отдаёт голый список
    без блока info, лимит записей на запрос нигде не указан. Используем безопасный дефолт
    (нарезка периода на окна по дням + дедупликация по uid) вместо оптимистичного «один запрос =
    все данные за период». Если на живом ключе подтвердится постраничная выдача — учтено ниже.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

import requests

log = logging.getLogger("kcell_client")

# Из документации известны эти значения status (кроме "success" — успешный дозвон).
# Всё остальное считаем попыткой (не критично для классификации), но логируем WARNING,
# чтобы не пропустить реальное новое значение статуса от Kcell молча.
_KNOWN_ATTEMPT_STATUSES = {"missed", "cancel", "busy", "notavailable", "notfound", "noanswer"}


class RecordUnavailable(RuntimeError):
    """У звонка нет записи (короткий/несостоявшийся разговор) — не пытаться скачивать."""


def _iso(v: str | None) -> str | None:
    """Kcell отдаёт UTC ISO с суффиксом Z ("2022-01-20T08:59:22Z") — приводим к формату,
    который понимает datetime.fromisoformat на всех поддерживаемых версиях Python."""
    if not v:
        return None
    return str(v).replace("Z", "+00:00")


def _digits(v: Any) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())


class KcellClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 60):
        # base_url: https://{домен}.vpbx.kcell.kz/crmapi/v1 (без слэша на конце)
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self.api_key}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = requests.get(f"{self.base}{path}", headers=self._headers(), params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # --- проверка ключа + таймзона портала (запускать перед первым боевым прогоном) ---
    def healthcheck(self) -> tuple[bool, str]:
        try:
            data = self._get("/domain")
            tz = (data or {}).get("timezone", {}) or {}
            return True, f"ok, timezone={tz.get('name')} offset={tz.get('offset')}"
        except Exception as e:  # noqa — здесь нужен именно текст причины, не трейсбек
            return False, str(e)

    # --- выгрузка звонков за период ---
    @staticmethod
    def _fmt_dt(dt: datetime) -> str:
        return dt.strftime("%Y%m%dT%H%M%SZ")

    def export(self, date_from: date, date_to: date, **kw: Any) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        day = date_from
        while day <= date_to:
            start = datetime.combine(day, time.min)
            end = datetime.combine(day, time.max)
            params: dict[str, Any] = {
                "start": self._fmt_dt(start), "end": self._fmt_dt(end),
                "type": kw.get("type", "all"),
            }
            for key in ("limit", "user", "diversion", "client", "groups",
                       "first_answered", "processMissed", "missedStatus"):
                if kw.get(key) is not None:
                    params[key] = kw[key]
            for it in self._history_page(params):
                uid = it.get("uid")
                if uid:
                    seen[uid] = it
            day += timedelta(days=1)
        return list(seen.values())

    def _history_page(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Один или несколько запросов /history/json по уже готовым start/end (день).
        Если ответ окажется постраничным (блок info.next) — доедаем; если это голый список,
        как в примере из документации, — один запрос и всё."""
        out: list[dict[str, Any]] = []
        resp = self._get("/history/json", params)
        items = resp.get("data", resp) if isinstance(resp, dict) else (resp or [])
        out.extend(items)
        info = resp.get("info") if isinstance(resp, dict) else None
        guard = 0
        while info and info.get("next") and guard < 1000:
            guard += 1
            params = dict(params, start=info["next"])
            resp = self._get("/history/json", params)
            items = resp.get("items", resp) if isinstance(resp, dict) else (resp or [])
            out.extend(items)
            info = resp.get("info") if isinstance(resp, dict) else None
        return out

    # --- скачать запись разговора ---
    def download_record(self, call_id: str, record_url: str | None = None) -> bytes:
        if not record_url:
            raise RecordUnavailable(f"нет ссылки на запись для звонка {call_id}")
        r = requests.get(record_url, headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.content

    # --- список сотрудников (замена sipuni.operators()) ---
    def operators(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = 0
        limit = 200
        while True:
            resp = self._get("/users", {"with": "status", "start": start, "limit": limit})
            items = resp.get("items", resp) if isinstance(resp, dict) else (resp or [])
            if not items:
                break
            out.extend(items)
            info = resp.get("info") if isinstance(resp, dict) else {}
            total = info.get("total")
            start += len(items)
            if len(items) < limit:
                break  # короче лимита — последняя страница
            if total is not None and start >= total:
                break
        return out

    # --- разбор строки истории -> унифицированные метаданные звонка для пайплайна ---
    @staticmethod
    def map_row(row: dict[str, Any]) -> dict[str, Any]:
        direction = "inbound" if (row.get("type") or "").lower() == "in" else "outbound"

        raw_status = (row.get("status") or "").strip()
        status_l = raw_status.lower()
        if status_l == "success":
            status = "success"
        elif not raw_status:
            status = "unknown"
        else:
            status = "attempt"
            if status_l not in _KNOWN_ATTEMPT_STATUSES:
                log.warning("Kcell: неизвестное значение status=%r (uid=%s) — считаю попыткой дозвона",
                           raw_status, row.get("uid"))

        return {
            "call_id": row.get("uid"),
            "datetime": _iso(row.get("start")),
            "direction": direction,
            "status": status,
            "operator_internal_number": None,  # у Kcell основной ключ привязки — логин, не номер
            "operator_login": row.get("user") or None,
            "client_number": _digits(row.get("client")),
            "duration_hint_sec": float(row.get("duration") or 0),
            "record_url": row.get("record") or None,
            "has_recording": bool(row.get("record")),
            "_raw": row,
        }
