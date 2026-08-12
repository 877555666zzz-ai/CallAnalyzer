"""
Фабрика клиента телефонии — переключение Sipuni/Kcell без правки pipeline.py/run_production.py.
Оба клиента (SipuniClient, KcellClient) реализуют один контракт:
    export(date_from, date_to, **kw) -> list[dict]
    download_record(call_id, record_url=None) -> bytes
    operators() -> list[dict]
    map_row(row: dict) -> dict  (call_id, datetime, direction, status,
                                  operator_internal_number, operator_login,
                                  client_number, duration_hint_sec, record_url, _raw)

TELEPHONY_PROVIDER=kcell (по умолчанию — целевой провайдер после миграции) | sipuni (легаси,
оставлен на время переходного периода, см. CLAUDE.md).
"""
from __future__ import annotations
import os
from typing import Any, Protocol


class TelephonyClient(Protocol):
    def export(self, date_from, date_to, **kw: Any) -> list[dict[str, Any]]: ...
    def download_record(self, call_id: str, record_url: str | None = None) -> bytes: ...
    def operators(self) -> list[dict[str, Any]]: ...

    @staticmethod
    def map_row(row: dict[str, Any]) -> dict[str, Any]: ...


def get_telephony_client(provider: str | None = None, env: dict[str, str] | None = None) -> TelephonyClient:
    env = env if env is not None else os.environ
    provider = (provider or env.get("TELEPHONY_PROVIDER") or "kcell").lower()

    if provider == "kcell":
        from .kcell_client import KcellClient
        return KcellClient(env["KCELL_BASE_URL"], env["KCELL_API_KEY"])
    if provider == "sipuni":
        from .sipuni_client import SipuniClient
        return SipuniClient(env["SIPUNI_USER"], env["SIPUNI_SECRET"])
    raise ValueError(f"неизвестный TELEPHONY_PROVIDER={provider!r} (ожидается kcell|sipuni)")
