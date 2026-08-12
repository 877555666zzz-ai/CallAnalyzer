"""
Адаптер Сипуни (ingest, §4 ТЗ). Контракт сверен с doc.sipuni.com:
  POST https://sipuni.com/api/statistic/export    — CSV звонков за период
  POST https://sipuni.com/api/statistic/record    — mp3 записи по id
  POST https://sipuni.com/api/statistic/operators  — CSV операторов
Подпись: MD5 от значений полей, склеенных через "+" в заданном порядке, в конце secret.

ЕДИНСТВЕННОЕ, что стоит проверить на вашем кабинете — названия колонок CSV (COLUMN_MAP ниже):
разные кабинеты Сипуни могут отдавать чуть разные заголовки. Парсер устойчив: лишние колонки
игнорируются, отсутствующие дают None. Сверьте один раз по реальной выгрузке и поправьте COLUMN_MAP.
"""
from __future__ import annotations
import csv
import hashlib
import io
from datetime import date, datetime
from typing import Any, Iterator

import requests

BASE = "https://sipuni.com/api/statistic"

# Реальные заголовки CSV кабинета (подтверждены боевой выгрузкой 4211 звонков).
COLUMN_MAP: dict[str, list[str]] = {
    "call_id":   ["ID записи"],
    "datetime":  ["Время"],
    "direction": ["Тип"],
    "status":    ["Статус"],
    "talked":    ["Кто разговаривал"],   # формат "205 (Дуба) 65"
    "answered":  ["Кто ответил"],
    "from":      ["Откуда"],             # исходящий: оператор "205 (Дуба)"; входящий: клиент
    "to":        ["Куда"],              # исходящий: клиент; входящий: внутр. линия
    "duration":  ["Длительность разговора, сек", "Длительность звонка, сек"],
    "scheme":    ["Схема"],
}

import re as _re
_EXT_RE = _re.compile(r"\b(\d{3,4})\b")        # внутренний номер оператора (3-4 цифры)
_PHONE_RE = _re.compile(r"\+?\d{6,}")           # телефон клиента (6+ цифр)


def _extract_ext(value: str | None) -> str | None:
    """Из '205 (Дуба) 65' / '205 (Дуба)' достаём внутренний номер оператора '205'."""
    if not value:
        return None
    m = _EXT_RE.search(value)
    return m.group(1) if m else None


def _extract_phone(value: str | None) -> str | None:
    if not value:
        return None
    m = _PHONE_RE.search(value.replace(" ", ""))
    return m.group(0) if m else None


def _md5(*parts: Any) -> str:
    return hashlib.md5("+".join(str(p) for p in parts).encode("utf-8")).hexdigest()


class SipuniClient:
    def __init__(self, user: str, secret: str, timeout: int = 60):
        self.user = str(user)
        self.secret = secret
        self.timeout = timeout

    # --- выгрузка звонков за период ---
    def export(self, date_from: date, date_to: date, *, call_type: str = "0",
               state: str = "0", tree: str = "", time_from: str = "", time_to: str = "",
               rating: str = "", hangupinitor: str = "1") -> list[dict[str, Any]]:
        d_from = date_from.strftime("%d.%m.%Y")
        d_to = date_to.strftime("%d.%m.%Y")
        # параметры по документации кабинета. anonymous=0 → выгружаем ВСЕ звонки;
        # names/numbersInvolved/numbersRinged=1 → в CSV попадут имена и номера сотрудников (для маппинга).
        p = {
            "anonymous": "0", "crmLinks": "0", "dtmfUserAnswer": "0", "firstTime": "0",
            "from": d_from, "fromNumber": "", "hangupinitor": hangupinitor, "ignoreSpecChar": "1",
            "names": "1", "numbersInvolved": "1", "numbersRinged": "1", "outgoingLine": "1",
            "rating": rating, "showTreeId": "0", "state": state,
            "timeFrom": time_from, "timeTo": time_to, "to": d_to,
            "toAnswer": "", "toNumber": "", "tree": tree, "type": call_type,
        }
        # ВАЖНО: для этой версии API кабинета crmLinks ВХОДИТ в подпись
        # (подтверждено тестом: вариант с crmLinks даёт HTTP 200), порядок алфавитный, затем user, secret.
        order = ["anonymous", "crmLinks", "dtmfUserAnswer", "firstTime", "from", "fromNumber",
                 "hangupinitor", "ignoreSpecChar", "names", "numbersInvolved", "numbersRinged",
                 "outgoingLine", "rating", "showTreeId", "state", "timeFrom", "timeTo", "to",
                 "toAnswer", "toNumber", "tree", "type"]
        p["user"] = self.user
        p["hash"] = _md5(*[p[k] for k in order], self.user, self.secret)
        r = requests.post(f"{BASE}/export", data=p, timeout=self.timeout)
        r.raise_for_status()
        return list(self._parse_csv(r.content))

    # --- выгрузка ВСЕХ звонков постранично ---
    def export_all(self, page: int = 1, limit: int = 200000, order: str = "asc") -> list[dict[str, Any]]:
        p = {"limit": limit, "order": order, "page": page, "user": self.user}
        p["hash"] = _md5(limit, order, page, self.user, self.secret)
        r = requests.post(f"{BASE}/export/all", data=p, timeout=self.timeout)
        r.raise_for_status()
        return list(self._parse_csv(r.content))

    # --- скачать запись разговора (mp3) ---
    def download_record(self, call_id: str, record_url: str | None = None) -> bytes:
        # record_url принимается для унификации интерфейса с KcellClient (см. src/telephony.py) —
        # у Sipuni запись качается по call_id через свой API, ссылка не нужна и игнорируется.
        p = {"id": call_id, "user": self.user, "hash": _md5(call_id, self.user, self.secret)}
        r = requests.post(f"{BASE}/record", data=p, timeout=self.timeout)
        r.raise_for_status()
        return r.content

    # --- список операторов (для маппинга внутр.номер -> ФИО) ---
    def operators(self) -> list[dict[str, Any]]:
        p = {"user": self.user, "hash": _md5(self.user, self.secret)}
        r = requests.post(f"{BASE}/operators", data=p, timeout=self.timeout)
        r.raise_for_status()
        return list(self._parse_csv(r.content))

    # --- разбор CSV ---
    @staticmethod
    def _parse_csv(content: bytes) -> Iterator[dict[str, Any]]:
        text = content.decode("utf-8-sig", errors="replace")
        sample = text[:2048]
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
        for row in csv.DictReader(io.StringIO(text), delimiter=delimiter):
            yield {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}

    @staticmethod
    def map_row(row: dict[str, Any]) -> dict[str, Any]:
        """CSV-строка -> унифицированные метаданные звонка для пайплайна."""
        def g(field: str):
            for cand in COLUMN_MAP[field]:
                if cand in row and row[cand] not in (None, ""):
                    return row[cand]
            return None

        raw_type = (g("direction") or "").lower()
        inbound = any(s in raw_type for s in ("вход", "in"))
        direction = "inbound" if inbound else "outbound"

        frm, to = g("from"), g("to")
        talked, answered = g("talked"), g("answered")

        if inbound:
            # клиент звонит «Откуда»; оператор — тот, кто разговаривал/ответил (внутр. номер)
            client = _extract_phone(frm)
            operator = _extract_ext(talked) or _extract_ext(answered) or _extract_ext(to)
        else:
            # исходящий: оператор в «Откуда» (205 (Дуба)) или «Кто разговаривал»; клиент в «Куда»
            client = _extract_phone(to) or _extract_phone(answered)
            operator = _extract_ext(frm) or _extract_ext(talked)

        return {
            "call_id": g("call_id"),
            "datetime": SipuniClient._parse_dt(g("datetime")),
            "direction": direction,
            "status": g("status"),
            "operator_internal_number": operator,
            "operator_login": None,   # у Sipuni операторы адресуются внутр. номером, логина нет
            "client_number": client,
            "duration_hint_sec": SipuniClient._parse_duration(g("duration")),
            "record_url": None,       # у Sipuni запись качается по call_id, а не по ссылке из строки
            "has_recording": True,    # Sipuni не даёт признак заранее — пробуем скачать, ошибку ловит retry/except
            "_raw": row,
        }

    @staticmethod
    def _parse_dt(v: str | None) -> str | None:
        if not v:
            return None
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y"):
            try:
                return datetime.strptime(v, fmt).isoformat()
            except ValueError:
                continue
        return v

    @staticmethod
    def _parse_duration(v: str | None) -> float | None:
        if not v:
            return None
        if ":" in v:  # формат ЧЧ:ММ:СС или ММ:СС
            parts = [int(x) for x in v.split(":")]
            sec = 0
            for x in parts:
                sec = sec * 60 + x
            return float(sec)
        try:
            return float(v)
        except ValueError:
            return None
