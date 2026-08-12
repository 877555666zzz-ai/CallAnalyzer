#!/usr/bin/env python3
"""
Ручной запуск миграции схемы manager/mapping_unmatched под Kcell (см. src/schema_migrate.py).

С версии, где src.db.get_sessionmaker() сам вызывает эту миграцию при каждом подключении,
этот скрипт не обязателен — БД самовосстанавливается автоматически. Оставлен для явного
просмотра плана и ручного контроля (например, перед первым прогоном на новой БД).

    source venv/bin/activate
    python tools/migrate_manager_keys.py            # показать план, ничего не менять
    python tools/migrate_manager_keys.py --apply    # применить явно (обычно не нужно)
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_env(path: str = ".env") -> None:
    p = ROOT / path
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    load_env()
    apply = "--apply" in sys.argv

    from src.db import get_engine
    from src.schema_migrate import ensure_manager_keys, plan_manager_keys

    engine = get_engine(os.environ.get("DATABASE_URL"))
    plan = plan_manager_keys(engine)

    if not plan:
        print("Миграция не нужна — схема уже актуальна.")
        return

    print("План:")
    for stmt in plan:
        print("  " + stmt)

    if not apply:
        print("\n(пробный режим). Чтобы применить: python tools/migrate_manager_keys.py --apply")
        print("(обычно не нужно — get_sessionmaker() применяет это автоматически при подключении)")
        return

    applied = ensure_manager_keys(engine)
    print(f"\n💾 Применено: {len(applied)} команд (+ индекс на kcell_login).")


if __name__ == "__main__":
    main()
