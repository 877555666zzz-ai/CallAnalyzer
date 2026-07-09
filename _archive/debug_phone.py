#!/usr/bin/env python3
"""Диагностика 2: телефон в компании или в названии сделки. Запуск из КОРНЯ."""
import os, sys, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for line in (ROOT/".env").read_text().splitlines():
    line=line.strip()
    if line and not line.startswith("#") and "=" in line:
        k,v=line.split("=",1); os.environ.setdefault(k.strip(), v.strip())
from src.bitrix_client import BitrixClient
from src.bitrix_sync import warm_deal_ids

def main():
    b = BitrixClient(os.environ["BITRIX_WEBHOOK"])
    ids = sorted(warm_deal_ids(b))
    print(f"Тёплых сделок: {len(ids)}. Проверяю первые 3...\n")
    for did in ids[:3]:
        deal = b.call("crm.deal.get", {"id": did}) or {}
        title = deal.get("TITLE", "")
        comp_id = deal.get("COMPANY_ID")
        print(f"--- Сделка {did} ---")
        print(f"  TITLE (название): {title!r}")
        # телефон компании
        if comp_id and str(comp_id) != "0":
            comp = b.call("crm.company.get", {"id": comp_id}) or {}
            print(f"  COMPANY {comp_id}: PHONE = {comp.get('PHONE')}")
            print(f"  COMPANY TITLE: {comp.get('TITLE')!r}")
        else:
            print("  компании нет")
        print()

if __name__ == "__main__":
    main()