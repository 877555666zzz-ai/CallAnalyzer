"""
Шаринг записей (§8.5): подписанные ссылки с ограничением по сроку, не публичные.
Токен = HMAC-SHA256(secret, recording_id|kind|expiry). Логирование доступа — в access_log.
"""
from __future__ import annotations
import os
import hmac
import hashlib
import base64
import time
from typing import Any

from .db import AccessLog


def _secret() -> bytes:
    return (os.environ.get("DASHBOARD_SECRET") or "dev-insecure-secret-change-me").encode()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def make_token(recording_id: str, kind: str = "original", ttl_sec: int = 86400) -> str:
    """Подписанный токен на запись с истечением (по умолчанию 24 ч)."""
    exp = int(time.time()) + ttl_sec
    payload = f"{recording_id}|{kind}|{exp}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return f"{_b64(payload.encode())}.{_b64(sig)}"


def verify_token(token: str) -> dict[str, Any] | None:
    """Вернёт {recording_id, kind, exp} если подпись верна и не истёк, иначе None."""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        pad = "=" * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode(payload_b64 + pad).decode()
        expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
        got = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        if not hmac.compare_digest(expected, got):
            return None
        recording_id, kind, exp = payload.split("|")
        if int(exp) < int(time.time()):
            return None
        return {"recording_id": recording_id, "kind": kind, "exp": int(exp)}
    except Exception:
        return None


def log_access(session, recording_id: str, actor: str, action: str, ip: str | None = None) -> None:
    session.add(AccessLog(recording_id=recording_id, actor=actor, action=action, ip=ip))
    session.commit()


def share_url(base: str, recording_id: str, kind: str = "original", ttl_sec: int = 86400) -> str:
    return f"{base.rstrip('/')}/r/{make_token(recording_id, kind, ttl_sec)}"
