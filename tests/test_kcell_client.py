#!/usr/bin/env python3
"""
Тест KcellClient.map_row на сохранённом дампе (tests/fixtures/kcell_history.json) —
без сети, без ключа. Плейн-скрипт (в проекте нет pytest), как test_deepgram.py/test_elevenlabs.py.

Запуск:
    source venv/bin/activate
    python tests/test_kcell_client.py
"""
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.kcell_client import KcellClient

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "kcell_history.json").read_text(encoding="utf-8"))


class _CaptureWarnings(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


def check(name: str, actual, expected) -> None:
    status = "ok" if actual == expected else "FAIL"
    print(f"  [{status}] {name}: {actual!r}" + ("" if actual == expected else f"  (ожидал {expected!r})"))
    assert actual == expected, f"{name}: {actual!r} != {expected!r}"


def main() -> None:
    print("=== звонок 1: успешный дозвон, есть запись ===")
    m = KcellClient.map_row(FIXTURE[0])
    check("call_id", m["call_id"], "20260811-000123")
    check("datetime", m["datetime"], "2026-08-11T08:59:22+00:00")
    check("direction", m["direction"], "outbound")
    check("status", m["status"], "success")
    check("operator_internal_number", m["operator_internal_number"], None)
    check("operator_login", m["operator_login"], "aisha")
    check("client_number", m["client_number"], "77029887766")
    check("duration_hint_sec", m["duration_hint_sec"], 87.0)
    check("record_url", m["record_url"],
         "https://records.vpbx.kcell.kz/aisha_out_2026_08_11-08_59_22_77029887766.mp3")
    check("has_recording", m["has_recording"], True)

    print("\n=== звонок 2: недозвон (NoAnswer), записи нет ===")
    m = KcellClient.map_row(FIXTURE[1])
    check("direction", m["direction"], "outbound")
    check("status", m["status"], "attempt")
    check("operator_login", m["operator_login"], "danna")
    check("duration_hint_sec", m["duration_hint_sec"], 0.0)
    check("record_url", m["record_url"], None)
    check("has_recording", m["has_recording"], False)

    print("\n=== звонок 3: пропущенный входящий на отдел (user пуст, group_name заполнен) ===")
    m = KcellClient.map_row(FIXTURE[2])
    check("direction", m["direction"], "inbound")
    check("status", m["status"], "attempt")
    check("operator_login", m["operator_login"], None)  # это и есть развилка "висит на отделе"
    check("client_number", m["client_number"], "77021234567")
    check("has_recording", m["has_recording"], False)

    print("\n=== звонок 4: неизвестное значение status — не должно падать, должен быть WARNING ===")
    handler = _CaptureWarnings()
    logging.getLogger("kcell_client").addHandler(handler)
    m = KcellClient.map_row(FIXTURE[3])
    logging.getLogger("kcell_client").removeHandler(handler)
    check("status", m["status"], "attempt")
    check("has_recording", m["has_recording"], True)
    assert any("WeirdNewStatus" in r for r in handler.records), \
        f"ожидал WARNING про неизвестный статус, получил: {handler.records}"
    print("  [ok] WARNING про неизвестный status залогирован, разбор не упал")

    print("\nВСЕ ПРОВЕРКИ ПРОШЛИ")


if __name__ == "__main__":
    main()
