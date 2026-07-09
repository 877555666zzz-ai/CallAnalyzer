"""
Диагностика: что РЕАЛЬНО отдаёт Сипуни на запрос записи.
Берёт первые несколько звонков и печатает статус, тип контента, размер, начало ответа.
    python debug_record.py
"""
import os, hashlib
from datetime import date, timedelta
from pathlib import Path
import requests
for line in Path(".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
from src.sipuni_client import SipuniClient

BASE = "https://sipuni.com/api/statistic"

def md5(*parts):
    return hashlib.md5("+".join(str(p) for p in parts).encode()).hexdigest()

def main():
    user = os.environ["SIPUNI_USER"]; secret = os.environ["SIPUNI_SECRET"]
    sip = SipuniClient(user, secret)
    d_to = date.today(); d_from = d_to - timedelta(days=1)
    rows = sip.export(d_from, d_to)

    # берём первые 5 звонков с длительностью
    cand = []
    for row in rows:
        meta = SipuniClient.map_row(row)
        if meta.get("call_id") and (meta.get("duration_hint_sec") or 0) >= 10:
            cand.append(meta["call_id"])
        if len(cand) >= 5:
            break

    print(f"Проверяю {len(cand)} звонков:\n")
    for cid in cand:
        p = {"id": cid, "user": user, "hash": md5(cid, user, secret)}
        r = requests.post(f"{BASE}/record", data=p, timeout=60)
        ctype = r.headers.get("Content-Type", "?")
        body = r.content
        preview = body[:200]
        print(f"call_id: {cid}")
        print(f"  HTTP {r.status_code} | Content-Type: {ctype} | размер: {len(body)} байт")
        print(f"  начало ответа: {preview[:150]!r}")
        print()

if __name__ == "__main__":
    main()