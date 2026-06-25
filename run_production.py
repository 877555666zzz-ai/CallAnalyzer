#!/usr/bin/env python3
"""
Боевой запуск за период из переменных окружения (.env).
Пример:
    python run_production.py 01.06.2026 24.06.2026

Что делает:
  - читает доступы из окружения;
  - выбирает LLM (Anthropic если есть ключ, иначе mock) и STT (STT_MODE);
  - тянет звонки из Сипуни за период, прогоняет весь конвейер, пишет в БД и (если задан) в Bitrix.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from src.analyzer import load_config
from src.llm_client import AnthropicClient, MockClient
from src.stt import get_engine as stt_engine
from src.sipuni_client import SipuniClient
from src.bitrix_client import BitrixClient
from src.db import get_engine, get_sessionmaker
from src.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent


def main():
    if len(sys.argv) < 3:
        print("usage: python run_production.py DD.MM.YYYY DD.MM.YYYY"); return
    d_from = datetime.strptime(sys.argv[1], "%d.%m.%Y").date()
    d_to = datetime.strptime(sys.argv[2], "%d.%m.%Y").date()

    cfg = load_config(ROOT / "configs" / "yandex_taxi_corp.yaml")
    Session = get_sessionmaker(get_engine(os.environ.get("DATABASE_URL")))

    llm = AnthropicClient() if os.environ.get("ANTHROPIC_API_KEY") else MockClient()
    stt = stt_engine(os.environ.get("STT_MODE", "mock"),
                     model_size=os.environ.get("WHISPER_MODEL", "large-v3"))

    sipuni = None
    if os.environ.get("SIPUNI_USER") and os.environ.get("SIPUNI_SECRET"):
        sipuni = SipuniClient(os.environ["SIPUNI_USER"], os.environ["SIPUNI_SECRET"])
    else:
        print("! SIPUNI_USER/SIPUNI_SECRET не заданы — нечего тянуть."); return

    bitrix = BitrixClient(os.environ["BITRIX_WEBHOOK"]) if os.environ.get("BITRIX_WEBHOOK") else None
    if bitrix:
        bitrix.ensure_userfields()  # одноразово создаст поля карточки

    pipe = Pipeline(cfg, Session, stt=stt, llm=llm, sipuni=sipuni, bitrix=bitrix,
                    dashboard_base=os.environ.get("DASHBOARD_BASE"))
    stats = pipe.process_period(d_from, d_to)
    print("Готово:", stats)


if __name__ == "__main__":
    main()
