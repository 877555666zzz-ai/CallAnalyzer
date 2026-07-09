#!/usr/bin/env python3
"""
Уборка проекта (безопасная): переносит отладочный мусор и устаревшие пилоты
в папку _archive/ вместо удаления. Ничего не теряется — если что-то нужно,
достанешь из _archive. Ядро src/, dashboard/, configs/, боевые скрипты и
записи звонков (out/audio, out/warm) НЕ трогаются.

    python3 cleanup.py          # показать план (dry-run)
    python3 cleanup.py --apply  # выполнить перенос
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "_archive"

# отладочные однодневки, ad-hoc тесты и пилоты, вытесненные боевыми скриптами
JUNK = [
    "debug_phone.py",       # диагностика телефонов
    "debug_record.py",      # диагностика записи Сипуни
    "test_match.py",        # ad-hoc проверка фильтра
    "test_warm.py",         # ad-hoc проверка «тёплых»
    "run_sample.py",        # ранний семпл-раннер
    "run_warm_pilot.py",    # пилот, вытеснен fetch_warm + run_production
    "fetch_one_record.py",  # вытеснен fetch_warm.py
    "bitrix_stages.py",     # разовая разведка стадий Bitrix
    "remove_managers.py",   # разовая чистка БД (setup_managers достаточно)
    ".DS_Store",
]

# демо-артефакты в out/ (пересоздаются; НЕ трогаем аудио и demo.db)
JUNK_OUT = [
    "out/demo-0001.analysis.json",
    "out/money_report.json",
    "out/sample_call_1782884206.326217.real.analysis.json",
]

KEEP_NOTE = [
    "out/demo.db — демо-база (нужна для показа дашборда без прод-данных)",
    "out/audio, out/warm — реальные записи звонков (нужны для теста прослушки)",
    "sample_call_1782884206.326217.mp3 — эталонный звонок для тестов STT",
]


def main() -> None:
    apply = "--apply" in sys.argv
    targets = [(ROOT / f, f) for f in JUNK] + [(ROOT / f, f) for f in JUNK_OUT]
    moved = 0
    print("=== План уборки (в _archive/, без удаления) ===" if apply else "=== DRY-RUN (ничего не меняю) ===")
    for path, rel in targets:
        if not path.exists():
            continue
        print(f"  {'ПЕРЕНОС' if apply else 'будет перенесён'}: {rel}")
        if apply:
            dst = ARCHIVE / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            path.rename(dst)
            moved += 1
    print("\n=== Оставляем как есть ===")
    for n in KEEP_NOTE:
        print(f"  • {n}")
    if apply:
        print(f"\nГотово. Перенесено в _archive/: {moved}. Проверь проект, потом можно удалить _archive вручную.")
    else:
        print("\nЭто был предпросмотр. Чтобы выполнить: python3 cleanup.py --apply")


if __name__ == "__main__":
    main()