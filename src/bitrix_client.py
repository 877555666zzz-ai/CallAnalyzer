"""
Адаптер Bitrix24 (§8.3 ТЗ) — пишет разбор звонка в карточку сделки.
Через входящий вебхук: https://{portal}.bitrix24.{tld}/rest/{user_id}/{code}/
Методы сверены с apidocs.bitrix24.com:
  crm.deal.userfield.add   — создать кастомные поля карточки (один раз)
  crm.deal.update          — записать в поля сделки
  crm.timeline.comment.add — добавить форматированный комментарий в таймлайн
  crm.duplicate.findbycomm — найти сделку по телефону клиента
"""
from __future__ import annotations
from typing import Any

import requests

# Кастомные поля, которые заведём в карточке сделки (FIELD_NAME -> подпись)
USERFIELDS = {
    "CALL_RESULT": "Анализатор: результат контакта",
    "CALL_REFUSAL": "Анализатор: причина отказа",
    "CALL_SCORE": "Анализатор: оценка звонка",
}


class BitrixError(RuntimeError):
    pass


class BitrixClient:
    def __init__(self, webhook_base: str, timeout: int = 30):
        # webhook_base: 'https://portal.bitrix24.kz/rest/1/abcdef' (без метода и слэша на конце)
        self.base = webhook_base.rstrip("/")
        self.timeout = timeout

    def call(self, method: str, payload: dict[str, Any]) -> Any:
        r = requests.post(f"{self.base}/{method}.json", json=payload, timeout=self.timeout)
        data = r.json()
        if "error" in data:
            raise BitrixError(f"{method}: {data.get('error')} {data.get('error_description', '')}")
        return data.get("result")

    # --- одноразовая настройка полей карточки ---
    def ensure_userfields(self) -> None:
        existing = {f.get("FIELD_NAME") for f in (self.call("crm.deal.userfield.list", {}) or [])}
        for name, label in USERFIELDS.items():
            full = f"UF_CRM_{name}"
            if full in existing:
                continue
            self.call("crm.deal.userfield.add", {"fields": {
                "FIELD_NAME": name,
                "USER_TYPE_ID": "double" if name == "CALL_SCORE" else "string",
                "EDIT_FORM_LABEL": {"ru": label}, "LIST_COLUMN_LABEL": {"ru": label},
                "SETTINGS": {"ROWS": 1},
            }})

    # --- найти сделку по номеру клиента ---
    def find_deal_by_phone(self, phone: str) -> int | None:
        dup = self.call("crm.duplicate.findbycomm", {"type": "PHONE", "values": [phone], "entity_type": "DEAL"})
        deals = (dup or {}).get("DEAL") or []
        if deals:
            return int(deals[0])
        # запасной путь: через контакт -> сделки контакта
        dup_c = self.call("crm.duplicate.findbycomm", {"type": "PHONE", "values": [phone], "entity_type": "CONTACT"})
        contacts = (dup_c or {}).get("CONTACT") or []
        if contacts:
            res = self.call("crm.deal.list", {"filter": {"CONTACT_ID": contacts[0]},
                                              "order": {"DATE_CREATE": "DESC"}, "select": ["ID"]})
            if res:
                return int(res[0]["ID"])
        return None

    # --- записать разбор в карточку ---
    def write_deal_card(self, deal_id: int, analysis: dict[str, Any],
                        transcript_url: str | None = None) -> None:
        rc = analysis["result_classification"]["primary"]
        passed = sum(1 for c in analysis["checklist"] if c.get("passed") is True)
        total = sum(1 for c in analysis["checklist"] if c.get("passed") is not None)
        score = round(100 * passed / total) if total else 0

        self.call("crm.deal.update", {"ID": deal_id, "FIELDS": {
            "UF_CRM_CALL_RESULT": rc,
            "UF_CRM_CALL_REFUSAL": analysis.get("refusal_reason") or "",
            "UF_CRM_CALL_SCORE": score,
        }, "PARAMS": {"REGISTER_SONET_EVENT": "N"}})

        self.call("crm.timeline.comment.add", {"fields": {
            "ENTITY_ID": deal_id, "ENTITY_TYPE": "deal",
            "COMMENT": self._bbcode(analysis, transcript_url),
        }})

    @staticmethod
    def _bbcode(a: dict[str, Any], transcript_url: str | None) -> str:
        m = a["metrics"]
        lines = ["[B]🤖 Разбор звонка (Анализатор)[/B]", "", a["summary"], ""]
        if a.get("refusal_reason"):
            lines += [f"[B]Причина отказа:[/B] {a['refusal_reason']}", ""]
        lines.append("[B]Соответствие скрипту:[/B]")
        lines.append("[LIST]")
        for c in a["checklist"]:
            mark = "✅" if c.get("passed") else ("➖" if c.get("passed") is None else "❌")
            score = f" ({c['score']}%)" if c.get("score") is not None else ""
            lines.append(f"[*]{mark} {c['label']}{score}")
        lines.append("[/LIST]")
        if a["redflags"]:
            lines.append("[B]🚩 Ред-флаги:[/B]")
            lines.append("[LIST]")
            for rf in a["redflags"]:
                who = "оператор" if rf["who"] == "operator" else "клиент"
                lines.append(f"[*][{rf['severity'].upper()}] {who}: «{rf['quote']}» — {rf['explanation']}")
            lines.append("[/LIST]")
        lines.append(
            f"[B]Метрики:[/B] talk оператора {m['talk_ratio_operator_pct']}% · "
            f"длит. {m['total_duration_sec']}с · макс. пауза {m['longest_pause_sec']}с"
        )
        if transcript_url:
            lines.append(f"[B]Транскрипт и аудио:[/B] [URL={transcript_url}]открыть[/URL]")
        return "\n".join(lines)
