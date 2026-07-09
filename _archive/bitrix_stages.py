#!/usr/bin/env python3
"""
Разведка Битрикса: показывает стадии воронки 'Продажи' с их точными ID,
чтобы фильтр 'Теплые лиды' ловил правильную стадию, а не наугад.
Запуск из КОРНЯ:  python bitrix_stages.py
"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for line in (ROOT/".env").read_text().splitlines():
    line=line.strip()
    if line and not line.startswith("#") and "=" in line:
        k,v=line.split("=",1); os.environ.setdefault(k.strip(), v.strip())
from src.bitrix_client import BitrixClient

def main():
    wh = os.environ.get("BITRIX_WEBHOOK")
    if not wh:
        print("! Нет BITRIX_WEBHOOK в .env"); return
    b = BitrixClient(wh)

    # 1) воронки (категории) сделок
    print("=== ВОРОНКИ (crm.category.list) ===")
    try:
        cats = b.call("crm.category.list", {"entityTypeId": 2}) or {}
        cat_list = cats.get("categories", cats) if isinstance(cats, dict) else cats
        for c in (cat_list or []):
            print(f"  categoryId={c.get('id')}  name={c.get('name')}")
    except Exception as e:
        print("  (не вышло:", e, ")")

    # 2) стадии — по каждой воронке
    print("\n=== СТАДИИ (crm.dealcategory.stage.list / crm.status.list) ===")
    try:
        # универсальный способ: статусы типа DEAL_STAGE
        statuses = b.call("crm.status.list", {"filter": {"ENTITY_ID": "DEAL_STAGE"}}) or []
        for s in statuses:
            print(f"  STATUS_ID={s.get('STATUS_ID')!r:32} NAME={s.get('NAME')!r}")
        # и стадии кастомных воронок (DEAL_STAGE_1, DEAL_STAGE_2, ...)
        for cat_id in range(0, 6):
            eid = "DEAL_STAGE" if cat_id == 0 else f"DEAL_STAGE_{cat_id}"
            st = b.call("crm.status.list", {"filter": {"ENTITY_ID": eid}}) or []
            for s in st:
                if "тепл" in str(s.get("NAME","")).lower() or "warm" in str(s.get("NAME","")).lower():
                    print(f"  >>> НАЙДЕНА ТЁПЛАЯ: воронка={cat_id} STATUS_ID={s.get('STATUS_ID')!r} NAME={s.get('NAME')!r}")
    except Exception as e:
        print("  (не вышло:", e, ")")

    # 3) доступен ли метод истории стадий
    print("\n=== ИСТОРИЯ СТАДИЙ (crm.stagehistory.list) ===")
    try:
        h = b.call("crm.stagehistory.list", {"entityTypeId": 2, "filter": {}, "select": ["ID","OWNER_ID","STAGE_ID","CREATED_TIME"], "start": 0})
        rows = h.get("items", h) if isinstance(h, dict) else h
        print(f"  метод работает, записей в первой странице: {len(rows or [])}")
        for r in (rows or [])[:3]:
            print("   пример:", r)
    except Exception as e:
        print("  (метод недоступен:", e, ")")

if __name__ == "__main__":
    main()