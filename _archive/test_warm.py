#!/usr/bin/env python3
"""Проверка фильтра 'Теплые лиды' (сейчас на стадии). Запуск из КОРНЯ."""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for line in (ROOT/".env").read_text().splitlines():
    line=line.strip()
    if line and not line.startswith("#") and "=" in line:
        k,v=line.split("=",1); os.environ.setdefault(k.strip(), v.strip())
from src.bitrix_client import BitrixClient
from src.bitrix_sync import warm_deal_ids, warm_phones

def main():
    b = BitrixClient(os.environ["BITRIX_WEBHOOK"])
    print("Сделки СЕЙЧАС на стадии «Теплые лиды» ...", flush=True)
    ids = warm_deal_ids(b, progress=True)
    print(f"\n✅ Тёплых сделок сейчас: {len(ids)}")
    print("ID:", sorted(ids))
    print("\nТяну их телефоны ...", flush=True)
    phones = warm_phones(b)
    print(f"✅ Телефонов тёплых лидов: {len(phones)}")
    print("Номера (последние 10 цифр):", sorted(phones))

if __name__ == "__main__":
    main()