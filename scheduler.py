#!/usr/bin/env python3
"""
Планировщик автозабора (§11): периодически тянет новые звонки из Сипуни и шлёт вечернюю сводку.
Лёгкая замена очереди для старта (без Redis). Для прод-нагрузки позже — Celery/RQ.

Запуск:  python scheduler.py
ENV:     INGEST_EVERY_MIN (по умолчанию 30), DAILY_SUMMARY_AT (HH:MM, по умолчанию 19:00)
"""
import os
import sys
import time
import logging
from datetime import datetime, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from src.analyzer import load_config
from src.llm_client import AnthropicClient, MockClient
from src.stt import get_engine as stt_engine
from src.sipuni_client import SipuniClient
from src.bitrix_client import BitrixClient
from src.db import get_engine, get_sessionmaker
from src.pipeline import Pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scheduler")
ROOT = Path(__file__).resolve().parent


def build_pipeline():
    cfg = load_config(ROOT / "configs" / "yandex_taxi_corp.yaml")
    Session = get_sessionmaker(get_engine(os.environ.get("DATABASE_URL")))
    llm = AnthropicClient() if os.environ.get("ANTHROPIC_API_KEY") else MockClient()
    stt = stt_engine(os.environ.get("STT_MODE", "mock"))
    sipuni = SipuniClient(os.environ["SIPUNI_USER"], os.environ["SIPUNI_SECRET"])
    bitrix = BitrixClient(os.environ["BITRIX_WEBHOOK"]) if os.environ.get("BITRIX_WEBHOOK") else None
    return cfg, Pipeline(cfg, Session, stt=stt, llm=llm, sipuni=sipuni, bitrix=bitrix,
                         dashboard_base=os.environ.get("DASHBOARD_BASE"))


def main():
    every = int(os.environ.get("INGEST_EVERY_MIN", "30"))
    summary_at = os.environ.get("DAILY_SUMMARY_AT", "19:00")
    _, pipe = build_pipeline()
    last_summary_day = None
    log.info("Планировщик запущен: ingest каждые %d мин, сводка в %s", every, summary_at)

    while True:
        try:
            today = date.today()
            stats = pipe.process_period(today, today)  # добор за сегодня (идемпотентно по merge)
            log.info("ingest: %s", stats)
        except Exception as e:  # noqa
            log.exception("ingest error: %s", e)

        now = datetime.now()
        if now.strftime("%H:%M") >= summary_at and last_summary_day != now.date():
            try:
                if os.environ.get("TELEGRAM_BOT_TOKEN"):
                    from src.telegram_report import main as send_summary
                    send_summary()
                last_summary_day = now.date()
            except Exception as e:  # noqa
                log.exception("summary error: %s", e)

        time.sleep(every * 60)


if __name__ == "__main__":
    main()
