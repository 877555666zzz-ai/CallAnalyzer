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

# Кандидаты заголовков CSV -> наше поле. Берётся первый найденный.
COLUMN_MAP: dict[str, list[str]] = {
    "call_id":         ["ID звонка", "ID", "Идентификатор", "id"],
    "datetime":        ["Время", "Дата", "Время звонка", "Дата и время"],
    "direction":       ["Тип", "Тип звонка", "Направление"],
    "operator_number": ["Кто ответил", "Внутренний номер", "Кто разговаривал", "Оператор"],
    "client_number":   ["Откуда", "Куда", "Номер клиента", "Клиент"],
    "duration":        ["Длительность разговора", "Длительность", "Время разговора"],
    "scheme":          ["Схема"],
}


def _md5(*parts: Any) -> str:
    return hashlib.md5("+".join(str(p) for p in parts).encode("utf-8")).hexdigest()


class SipuniClient:
    def __init__(self, user: str, secret: str, timeout: int = 60):
        self.user = str(user)
        self.secret = secret
        self.timeout = timeout

    # --- выгрузка звонков за период ---
    def export(self, date_from: date, date_to: date, *, call_type: str = "0",
               state: str = "0", tree: str = "") -> list[dict[str, Any]]:
        d_from = date_from.strftime("%d.%m.%Y")
        d_to = date_to.strftime("%d.%m.%Y")
        # дефолты совпадают с примером из документации Сипуни
        p = {
            "anonymous": "1", "dtmfUserAnswer": "0", "firstTime": "0",
            "from": d_from, "fromNumber": "", "names": "0", "numbersInvolved": "0",
            "numbersRinged": "0", "outgoingLine": "1", "showTreeId": "1", "state": state,
            "to": d_to, "toAnswer": "", "toNumber": "", "tree": tree, "type": call_type,
        }
        # порядок подписи строго как в документации
        order = ["anonymous", "dtmfUserAnswer", "firstTime", "from", "fromNumber", "names",
                 "numbersInvolved", "numbersRinged", "outgoingLine", "showTreeId", "state",
                 "to", "toAnswer", "toNumber", "tree", "type"]
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
    def download_record(self, call_id: str) -> bytes:
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
        def pick(field: str):
            for cand in COLUMN_MAP[field]:
                if cand in row and row[cand] not in (None, ""):
                    return row[cand]
            return None

        raw_type = (pick("direction") or "").lower()
        direction = "inbound" if any(s in raw_type for s in ("вход", "in")) else "outbound"
        return {
            "call_id": pick("call_id"),
            "datetime": SipuniClient._parse_dt(pick("datetime")),
            "direction": direction,
            "operator_internal_number": pick("operator_number"),
            "client_number": pick("client_number"),
            "duration_hint_sec": SipuniClient._parse_duration(pick("duration")),
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
