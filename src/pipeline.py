#!/usr/bin/env python3
"""
Оркестратор (§3, §11). Сквозной путь одного звонка:
  телефония(метаданные+запись) -> STT(segments) -> LLM-анализ(JSON) -> БД -> [карточка Bitrix]

Провайдер телефонии (Sipuni/Kcell) подставляется через src.telephony.get_telephony_client() —
pipeline.py от конкретного провайдера не зависит, работает через общий контракт (см. telephony.py).

Боевой режим:  process_period(date_from, date_to) — тянет звонки за период.
Демо-режим:    python pipeline.py --demo — гонит sample-звонок через весь путь на моках.

Несвязанные по логину/номеру оператора звонки уходят в mapping_unmatched (ничего не теряем, §4.2).
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
from src.telephony import TelephonyClient
from src.bitrix_client import BitrixClient
from src.db import get_engine as db_engine, get_sessionmaker, Base
from src import store
from src.retry import retry

log = logging.getLogger("pipeline")
ROOT = Path(__file__).resolve().parent.parent


class Pipeline:
    def __init__(self, cfg: dict[str, Any], session_factory, stt: STTEngine, llm: BaseLLMClient,
                 telephony: TelephonyClient | None = None, bitrix: BitrixClient | None = None,
                 storage_dir: Path | None = None, dashboard_base: str | None = None,
                 restrict_to_managed: bool = True, allowed_numbers: set[str] | None = None):
        self.cfg = cfg
        self.Session = session_factory
        self.stt = stt
        self.llm = llm
        self.telephony = telephony
        self.bitrix = bitrix
        self.storage = storage_dir or (ROOT / "out" / "audio")
        self.storage.mkdir(parents=True, exist_ok=True)
        self.dashboard_base = dashboard_base
        # Фильтр «в зоне»: обрабатываем только звонки известных менеджеров (напр. 19 продажников).
        # allowed_numbers=None -> берём из таблицы manager (кого залил setup_managers.py).
        # Чужие логины/номера (другие отделы, боты, шум АТС) пропускаем БЕЗ затрат на STT/LLM.
        # restrict_to_managed=False -> старое поведение: тянуть всё, чужие в mapping_unmatched.
        self.restrict_to_managed = restrict_to_managed
        self.allowed_numbers = set(allowed_numbers) if allowed_numbers else None

    # --- боевой проход за период ---
    def process_period(self, date_from: date, date_to: date, limit: int | None = None) -> dict[str, int]:
        """limit — сколько звонков РЕАЛЬНО прогнать через STT+LLM (платные вызовы).
        Пропущенные/вне-зоны/без-записи звонки в лимит не считаются — только успешно обработанные."""
        assert self.telephony, "клиент телефонии не сконфигурирован (см. src.telephony.get_telephony_client)"
        rows = retry(attempts=3)(self.telephony.export)(date_from, date_to)
        stats = {"total": 0, "ok": 0, "unmatched": 0, "no_audio": 0, "skipped": 0, "errors": 0}
        started_at = datetime.utcnow()
        with self.Session() as s:
            allow = self.allowed_numbers
            if self.restrict_to_managed and allow is None:
                allow = store.managed_keys(s)  # напр. 19 продажников из таблицы manager
            for row in rows:
                if limit is not None and stats["ok"] >= limit:
                    break
                stats["total"] += 1
                # фильтр по оператору: чужой логин/номер = вне зоны, пропускаем без STT/LLM
                if allow is not None:
                    meta = self.telephony.map_row(row)
                    key = meta.get("operator_login") or meta.get("operator_internal_number")
                    if key not in allow:
                        stats["skipped"] += 1
                        continue
                outcome = "errors"
                try:
                    outcome = self._process_row(s, row)
                    stats[outcome] += 1
                except Exception as e:  # noqa — один битый звонок не валит пакет
                    stats["errors"] += 1
                    log.exception("call failed: %s", e)
                # Прогресс построчно — на долгих прогонах (десятки-сотни звонков) без этого
                # непонятно, сколько осталось и не завис ли процесс. limit считает только "ok",
                # поэтому ETA тоже считаем от него, а не от общего числа строк в периоде.
                if limit is not None:
                    elapsed_min = (datetime.utcnow() - started_at).total_seconds() / 60
                    rate = stats["ok"] / elapsed_min if elapsed_min > 0 else 0
                    eta = f"{(limit - stats['ok']) / rate:.0f} мин" if rate > 0 else "?"
                    print(f"[{stats['ok']}/{limit}] {outcome} "
                          f"ok={stats['ok']} no_audio={stats['no_audio']} unmatched={stats['unmatched']} "
                          f"errors={stats['errors']} | ETA ~{eta}", flush=True)
        return stats

    # --- обработать один звонок вне цикла process_period — для вебхука (§ вторым этапом) ---
    def process_single(self, row: dict[str, Any]) -> str:
        """Возвращает 'ok' | 'unmatched' | 'no_audio' | 'skipped' (оператор вне зоны)."""
        assert self.telephony, "клиент телефонии не сконфигурирован"
        with self.Session() as s:
            allow = self.allowed_numbers
            if self.restrict_to_managed and allow is None:
                allow = store.managed_keys(s)
            if allow is not None:
                meta = self.telephony.map_row(row)
                key = meta.get("operator_login") or meta.get("operator_internal_number")
                if key not in allow:
                    return "skipped"
            return self._process_row(s, row)

    def _process_row(self, session, row: dict[str, Any]) -> str:
        """Возвращает 'ok' | 'unmatched' | 'no_audio'."""
        meta = self.telephony.map_row(row)
        call_id = meta["call_id"]
        key = meta.get("operator_login") or meta.get("operator_internal_number")
        started = datetime.fromisoformat(meta["datetime"]) if meta["datetime"] else datetime.utcnow()

        manager = store.find_manager(session, key) if key else None
        if manager is None:
            store.record_unmatched(session, call_id, key, started, "operator_not_mapped")
            return "unmatched"

        # Экономия: недозвоны, звонки без записи и слишком короткие для речи (см. min_billable_
        # duration_sec — проверено на живых данных 10-12.08.2026, см. комментарий в конфиге)
        # не должны доходить до платных STT/LLM.
        duration = meta.get("duration_hint_sec") or 0
        min_duration = self.cfg.get("metrics", {}).get("min_billable_duration_sec", 0)
        if duration <= 0 or duration < min_duration or not meta.get("has_recording", True):
            return "no_audio"

        audio_path = self.storage / f"{call_id}.mp3"
        audio_bytes = retry(attempts=3)(self.telephony.download_record)(call_id, meta.get("record_url"))
        audio_path.write_bytes(audio_bytes)

        channel = self.cfg.get("default_channel", "stereo")
        segments = retry(attempts=2)(self.stt.transcribe)(str(audio_path), channel)

        call = {"call_id": call_id, "metadata": {
            "datetime": started.isoformat(), "direction": meta["direction"],
            "operator_internal_number": meta.get("operator_internal_number"),
            "operator_login": meta.get("operator_login"),
            "operator_name": manager.full_name,
            "department": manager.department, "project": manager.project,
            "client_number": meta["client_number"], "channel": channel,
            "audio_url": str(audio_path),
        }, "segments": segments}

        analysis = analyze_call(call, self.cfg, self.llm)
        store.save_call_with_analysis(session, call, analysis)
        self._push_bitrix(call, analysis)
        return "ok"

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
        store.upsert_manager(s, "Айгерим", "sales_taxi", "yandex_taxi_corp", internal_number="234")
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