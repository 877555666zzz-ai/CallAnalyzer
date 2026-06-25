#!/usr/bin/env python3
"""
Оркестратор (§3, §11). Сквозной путь одного звонка:
  Сипуни(метаданные+запись) -> STT(segments) -> LLM-анализ(JSON) -> БД -> [карточка Bitrix]

Боевой режим:  process_period(date_from, date_to) — тянет звонки из Сипуни за период.
Демо-режим:    python pipeline.py --demo — гонит sample-звонок через весь путь на моках.

Несвязанные по внутреннему номеру звонки уходят в mapping_unmatched (ничего не теряем, §4.2).
"""
from __future__ import annotations
import os
import sys
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.analyzer import analyze_call, load_config
from src.llm_client import BaseLLMClient, MockClient, AnthropicClient
from src.stt import STTEngine, MockSTT, get_engine
from src.sipuni_client import SipuniClient
from src.bitrix_client import BitrixClient
from src.db import get_engine as db_engine, get_sessionmaker, Base
from src import store
from src.retry import retry

log = logging.getLogger("pipeline")
ROOT = Path(__file__).resolve().parent.parent


class Pipeline:
    def __init__(self, cfg: dict[str, Any], session_factory, stt: STTEngine, llm: BaseLLMClient,
                 sipuni: SipuniClient | None = None, bitrix: BitrixClient | None = None,
                 storage_dir: Path | None = None, dashboard_base: str | None = None):
        self.cfg = cfg
        self.Session = session_factory
        self.stt = stt
        self.llm = llm
        self.sipuni = sipuni
        self.bitrix = bitrix
        self.storage = storage_dir or (ROOT / "out" / "audio")
        self.storage.mkdir(parents=True, exist_ok=True)
        self.dashboard_base = dashboard_base

    # --- боевой проход за период ---
    def process_period(self, date_from: date, date_to: date) -> dict[str, int]:
        assert self.sipuni, "SipuniClient не сконфигурирован"
        rows = retry(attempts=3)(self.sipuni.export)(date_from, date_to)
        stats = {"total": 0, "ok": 0, "unmatched": 0, "errors": 0}
        with self.Session() as s:
            for row in rows:
                stats["total"] += 1
                try:
                    if self._process_row(s, row):
                        stats["ok"] += 1
                    else:
                        stats["unmatched"] += 1
                except Exception as e:  # noqa — один битый звонок не валит пакет
                    stats["errors"] += 1
                    log.exception("call failed: %s", e)
        return stats

    def _process_row(self, session, row: dict[str, Any]) -> bool:
        meta = SipuniClient.map_row(row)
        call_id, internal = meta["call_id"], meta["operator_internal_number"]
        started = datetime.fromisoformat(meta["datetime"]) if meta["datetime"] else datetime.utcnow()

        manager = store.find_manager(session, internal) if internal else None
        if manager is None:
            store.record_unmatched(session, call_id, internal, started, "internal_number_not_mapped")
            return False

        audio_path = self.storage / f"{call_id}.mp3"
        audio_bytes = retry(attempts=3)(self.sipuni.download_record)(call_id)
        audio_path.write_bytes(audio_bytes)

        channel = self.cfg.get("default_channel", "stereo")
        segments = retry(attempts=2)(self.stt.transcribe)(str(audio_path), channel)

        call = {"call_id": call_id, "metadata": {
            "datetime": started.isoformat(), "direction": meta["direction"],
            "operator_internal_number": internal, "operator_name": manager.full_name,
            "department": manager.department, "project": manager.project,
            "client_number": meta["client_number"], "channel": channel,
            "audio_url": str(audio_path),
        }, "segments": segments}

        analysis = analyze_call(call, self.cfg, self.llm)
        store.save_call_with_analysis(session, call, analysis)
        self._push_bitrix(call, analysis)
        return True

    def _push_bitrix(self, call: dict[str, Any], analysis: dict[str, Any]) -> None:
        if not self.bitrix:
            return
        try:
            deal_id = self.bitrix.find_deal_by_phone(call["metadata"]["client_number"])
            if not deal_id:
                log.info("no deal for %s", call["metadata"]["client_number"])
                return
            url = f"{self.dashboard_base}/calls/{call['call_id']}" if self.dashboard_base else None
            self.bitrix.write_deal_card(deal_id, analysis, transcript_url=url)
        except Exception as e:  # noqa — Bitrix не должен ронять обработку звонка
            log.warning("bitrix push failed: %s", e)


# ---------------- демо ----------------
def _demo() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config(ROOT / "configs" / "yandex_taxi_corp.yaml")
    sample = json.loads((ROOT / "sample" / "call_demo_ru_kz.json").read_text(encoding="utf-8"))

    engine = db_engine("sqlite:///" + str(ROOT / "out" / "demo.db"))
    Session = get_sessionmaker(engine)
    with Session() as s:
        store.upsert_manager(s, "Айгерим", "234", "sales_taxi", "yandex_taxi_corp")
        s.commit()

    # STT-мок отдаёт готовые сегменты sample-звонка (в бою сюда встанет WhisperSTT)
    pipe = Pipeline(cfg, Session, stt=MockSTT(sample["segments"]), llm=MockClient())

    with Session() as s:
        call = {"call_id": "demo-pipeline-1", "metadata": {**sample["metadata"]}, "segments": []}
        # эмулируем путь: STT -> analyze -> store (без реального Сипуни/аудио)
        call["segments"] = pipe.stt.transcribe("(mock)", call["metadata"].get("channel", "stereo"))
        analysis = analyze_call(call, cfg, pipe.llm)
        store.save_call_with_analysis(s, call, analysis)
        log.info("OK: звонок %s обработан и сохранён (result=%s, флагов=%d)",
                 call["call_id"], analysis["result_classification"]["primary"], len(analysis["redflags"]))


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _demo()
    else:
        print("Боевой режим: импортируйте Pipeline и вызовите process_period(date_from, date_to).")
        print("Демо: python pipeline.py --demo")
