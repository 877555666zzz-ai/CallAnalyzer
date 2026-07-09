"""
Скачать ОДНУ запись звонка из Сипуни для теста Deepgram. Запускать из КОРНЯ проекта.
    python fetch_one_record.py          # за последний 1 день
    python fetch_one_record.py 3        # за 3 дня
"""
import os, sys
from datetime import date, timedelta
from pathlib import Path
for line in Path(".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
from src.sipuni_client import SipuniClient

MAX_TRIES = 60

def is_mp3(b: bytes) -> bool:
    # валидный mp3: заголовок ID3 или любой аудиофрейм \xff\xEx/\xFx (разный битрейт/версия)
    return bool(b) and len(b) > 2000 and (b[:3] == b"ID3" or (b[0] == 0xFF and (b[1] & 0xE0) == 0xE0))

def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    user = os.environ.get("SIPUNI_USER"); secret = os.environ.get("SIPUNI_SECRET")
    if not (user and secret):
        print("! Нет SIPUNI_USER / SIPUNI_SECRET в .env"); return
    sip = SipuniClient(user, secret)
    d_to = date.today(); d_from = d_to - timedelta(days=days)
    print(f"Тяну звонки {d_from:%d.%m.%Y} - {d_to:%d.%m.%Y} ...")
    rows = sip.export(d_from, d_to)
    print(f"Всего звонков за период: {len(rows)}")

    cand = []
    for row in rows:
        meta = SipuniClient.map_row(row)
        dur = meta.get("duration_hint_sec") or 0
        if meta.get("call_id") and dur and dur >= 15:
            cand.append((dur, meta))
    cand.sort(key=lambda x: x[0], reverse=True)
    print(f"Кандидатов с длительностью >=15с: {len(cand)}. Пробую скачать (макс {MAX_TRIES})...")

    for i, (dur, meta) in enumerate(cand[:MAX_TRIES], 1):
        call_id = meta["call_id"]
        try:
            audio = sip.download_record(call_id)
        except Exception as e:
            print(f"  [{i}] {call_id}: ошибка ({e})"); continue
        if is_mp3(audio):
            out = Path(f"sample_call_{call_id}.mp3"); out.write_bytes(audio)
            print(f"\nСохранено: {out}  ({len(audio)//1024} КБ, "
                  f"оператор={meta.get('operator_internal_number')}, {meta.get('direction')}, ~{int(dur)}с)")
            print(f"\nТеперь протестируй:\n    python test_deepgram.py {out}")
            return
        print(f"  [{i}] {call_id}: пусто ({len(audio)} байт)")
    print("\n! Записей не нашёл. Попробуй больше дней: python fetch_one_record.py 3")

if __name__ == "__main__":
    main()