#!/usr/bin/env python3
"""
Проверка фильтра: сколько РЕАЛЬНЫХ звонков попадают в тёплых лидов по номеру.
Сипуни (звонки) ∩ Битрикс (тёплые номера). Запуск из КОРНЯ.
    python test_match.py        # за 2 дня
    python test_match.py 7      # за 7 дней
"""
import os, sys
from datetime import date, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for line in (ROOT/".env").read_text().splitlines():
    line=line.strip()
    if line and not line.startswith("#") and "=" in line:
        k,v=line.split("=",1); os.environ.setdefault(k.strip(), v.strip())
from src.sipuni_client import SipuniClient
from src.bitrix_client import BitrixClient
from src.bitrix_sync import warm_phones, _digits

def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 2

    # 1) тёплые номера из Битрикса
    print("Тяну тёплые номера из Битрикса ...", flush=True)
    b = BitrixClient(os.environ["BITRIX_WEBHOOK"])
    warm = warm_phones(b)
    print(f"  тёплых номеров: {len(warm)}")

    # 2) звонки из Сипуни
    print(f"Тяну звонки из Сипуни за {days} дн. ...", flush=True)
    sip = SipuniClient(os.environ["SIPUNI_USER"], os.environ["SIPUNI_SECRET"])
    d_to = date.today(); d_from = d_to - timedelta(days=days)
    rows = sip.export(d_from, d_to)
    print(f"  звонков всего: {len(rows)}")

    # 3) пересечение по последним 10 цифрам
    matched = []
    call_numbers = set()
    for row in rows:
        meta = SipuniClient.map_row(row)
        cn = _digits(meta.get("client_number"))
        if len(cn) >= 10:
            cn10 = cn[-10:]
            call_numbers.add(cn10)
            if cn10 in warm:
                matched.append((cn10, meta.get("operator_internal_number"), meta.get("call_id")))

    print(f"\n=== РЕЗУЛЬТАТ ===")
    print(f"Уникальных номеров в звонках: {len(call_numbers)}")
    print(f"Звонков от ТЁПЛЫХ лидов: {len(matched)}")
    if matched:
        print("Примеры совпадений (номер / оператор / call_id):")
        for m in matched[:10]:
            print(f"  {m[0]}  оп={m[1]}  id={m[2]}")
    else:
        print("Совпадений нет за этот период (тёплые лиды могли звонить раньше).")
        print("Попробуй больше дней: python test_match.py 14")

if __name__ == "__main__":
    main()