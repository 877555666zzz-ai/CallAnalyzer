#!/usr/bin/env python3
"""
ФИНАЛ: полная система на реальных ТЁПЛЫХ звонках.
Сипуни → отбор тёплых по номеру (Битрикс) → скачать запись → Deepgram → Gemini → разбор.
Запуск из КОРНЯ:
    python run_warm_pilot.py          # за 2 дня, максимум 3 звонка
    python run_warm_pilot.py 7 5      # за 7 дней, максимум 5 звонков
"""
import os, sys, json
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
from src.stt import DeepgramSTT
from src.analyzer import analyze_call, load_config
from src.llm_client import GeminiClient

MP3 = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
def is_mp3(b): return bool(b) and len(b) > 2000 and (b[:3]==b"ID3" or (b[0]==0xFF and (b[1]&0xE0)==0xE0))

def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    b = BitrixClient(os.environ["BITRIX_WEBHOOK"])
    print("Тяну тёплые номера ...", flush=True)
    warm = warm_phones(b)
    print(f"  тёплых номеров: {len(warm)}")

    sip = SipuniClient(os.environ["SIPUNI_USER"], os.environ["SIPUNI_SECRET"])
    d_to = date.today(); d_from = d_to - timedelta(days=days)
    print(f"Тяну звонки за {days} дн. ...", flush=True)
    rows = sip.export(d_from, d_to)

    # отбираем звонки от тёплых, с длительностью
    warm_calls = []
    for row in rows:
        m = SipuniClient.map_row(row)
        cn = _digits(m.get("client_number"))
        dur = m.get("duration_hint_sec") or 0
        if len(cn) >= 10 and cn[-10:] in warm and dur >= 15 and m.get("call_id"):
            warm_calls.append(m)
    print(f"  тёплых звонков с записью: {len(warm_calls)}\n")

    stt = DeepgramSTT(os.environ["DEEPGRAM_API_KEY"],
                      model=os.environ.get("DEEPGRAM_MODEL","nova-3"),
                      language=os.environ.get("DEEPGRAM_LANG","multi"))
    llm = GeminiClient()
    cfg = load_config(ROOT / "configs" / "yandex_taxi_corp.yaml")

    done = 0
    for m in warm_calls:
        if done >= limit:
            break
        cid = m["call_id"]
        print("="*64)
        print(f"ЗВОНОК {cid} | клиент {m.get('client_number')} | оператор {m.get('operator_internal_number')}")
        print("="*64)
        try:
            audio = sip.download_record(cid)
        except Exception as e:
            print(f"  запись не скачалась: {e}\n"); continue
        if not is_mp3(audio):
            print("  записи нет, пропуск\n"); continue
        f = ROOT / "out" / f"warm_{cid}.mp3"; f.write_bytes(audio)

        print("  [1/2] Deepgram ...", flush=True)
        segs = stt.transcribe(str(f), channel="mono")
        print(f"        реплик: {len(segs)}")
        if not segs:
            print("  пустой транскрипт\n"); continue

        call = {"call_id": cid,
                "metadata": {"direction": m.get("direction","outbound"),
                             "operator_internal_number": m.get("operator_internal_number","?"),
                             "operator_name":"?","department":"sales_taxi",
                             "project":"yandex_taxi_corp",
                             "client_number": m.get("client_number","?"),"channel":"mono"},
                "segments": segs}

        print("  [2/2] Gemini ...", flush=True)
        try:
            a = analyze_call(call, cfg, llm)
        except Exception as e:
            print(f"  анализ не удался: {e}\n"); continue

        (ROOT/"out"/f"warm_{cid}.analysis.json").write_text(
            json.dumps(a, ensure_ascii=False, indent=2), encoding="utf-8")

        print("\n  ИТОГ:", a.get("summary","-"))
        rc = a.get("result_classification",{})
        print("  РЕЗУЛЬТАТ:", rc.get("primary","-"), "| увер:", rc.get("confidence","-"))
        print("  ЭТАП ПОТЕРИ:", a.get("loss_stage","-"))
        rf = a.get("redflags",[])
        print(f"  КРАСНЫЕ ФЛАГИ: {len(rf)}")
        for r in rf:
            print(f"    ⚠️  [{r.get('severity')}] {r.get('explanation','')[:90]}")
        print()
        done += 1

    print("="*64)
    print(f"Готово. Разобрано тёплых звонков: {done}")
    print("Полные разборы: out/warm_*.analysis.json")

if __name__ == "__main__":
    main()