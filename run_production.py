#!/usr/bin/env python3
"""
Боевой запуск за период. Автозагрузка .env, анализ через Gemini (бесплатный тир),
STT-роутер (route): русский -> Deepgram, казах -> ElevenLabs.

Пример:
    source venv/bin/activate
    python3 run_production.py 07.07.2026 09.07.2026
"""
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# --- Автозагрузка .env (не перетираем уже заданные переменные) ---
_env = ROOT / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from src.analyzer import load_config
from src.runtime import build_llm, build_stt
from src.sipuni_client import SipuniClient
from src.bitrix_client import BitrixClient
from src.bitrix_sync import sync_deals
from src.db import get_engine, get_sessionmaker
from src.pipeline import Pipeline


def main():
    if len(sys.argv) < 3:
        print("usage: python3 run_production.py DD.MM.YYYY DD.MM.YYYY [--limit N]"); return
    d_from = datetime.strptime(sys.argv[1], "%d.%m.%Y").date()
    d_to = datetime.strptime(sys.argv[2], "%d.%m.%Y").date()
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    cfg = load_config(ROOT / "configs" / "yandex_taxi_corp.yaml")
    Session = get_sessionmaker(get_engine(os.environ.get("DATABASE_URL")))

    llm, llm_name = build_llm()
    print(f"LLM: {llm_name}" + (" (нет ключей)" if llm_name == "mock" else ""))

    stt_mode = os.environ.get("STT_MODE", "mock")
    stt, _ = build_stt()
    print(f"STT: {stt_mode}")

    if os.environ.get("SIPUNI_USER") and os.environ.get("SIPUNI_SECRET"):
        sipuni = SipuniClient(os.environ["SIPUNI_USER"], os.environ["SIPUNI_SECRET"])
    else:
        print("! SIPUNI_USER/SIPUNI_SECRET не заданы — нечего тянуть."); return

    bitrix = BitrixClient(os.environ["BITRIX_WEBHOOK"]) if os.environ.get("BITRIX_WEBHOOK") else None
    if bitrix:
        bitrix.ensure_userfields()

    pipe = Pipeline(cfg, Session, stt=stt, llm=llm, sipuni=sipuni, bitrix=bitrix,
                    dashboard_base=os.environ.get("DASHBOARD_BASE"))
    stats = pipe.process_period(d_from, d_to, limit=limit)
    print("Готово:", stats)

    if bitrix:
        with Session() as s:
            n = sync_deals(bitrix, s, project=cfg["project"])
        print(f"Bitrix deal sync: {n} сделок (для /conversions, /boss)")


if __name__ == "__main__":
    main()