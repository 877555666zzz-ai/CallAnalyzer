#!/usr/bin/env python3
"""
Одноразовая миграция схемы БД под Kcell:
  manager.sipuni_internal_number          -> manager.internal_number (+ добавляет manager.kcell_login)
  mapping_unmatched.sipuni_internal_number -> mapping_unmatched.internal_number

Нужна потому, что таблицы в проекте создаются через Base.metadata.create_all (без Alembic) —
переименование колонки в src/db.py само по себе на уже существующей БД (боевой Postgres на
Railway, локальный SQLite) ничего не поменяет, create_all не трогает существующие таблицы.

Идемпотентно — можно гонять сколько угодно раз, ничего не сломает, если уже применено.
Работает и на SQLite (3.25+, стандартно для Python), и на Postgres — синтаксис
ALTER TABLE ... RENAME COLUMN одинаковый у обоих.

    source venv/bin/activate
    python tools/migrate_manager_keys.py            # показать план, ничего не менять
    python tools/migrate_manager_keys.py --apply    # применить
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

    from sqlalchemy import inspect, text
    from src.db import get_engine

    engine = get_engine(os.environ.get("DATABASE_URL"))
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())

    plan: list[str] = []
    for table, old, new in [
        ("manager", "sipuni_internal_number", "internal_number"),
        ("mapping_unmatched", "sipuni_internal_number", "internal_number"),
    ]:
        if table not in existing_tables:
            print(f"[skip] таблицы {table} ещё нет — create_all создаст её сразу в новой схеме")
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if new in cols:
            print(f"[ok] {table}.{new} уже существует — переименование не нужно")
        elif old in cols:
            plan.append(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new};")
        else:
            print(f"[warn] в {table} нет ни {old}, ни {new} — руками проверить схему")

    if "manager" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("manager")}
        if "kcell_login" not in cols:
            plan.append("ALTER TABLE manager ADD COLUMN kcell_login VARCHAR(64);")
        else:
            print("[ok] manager.kcell_login уже существует")

    if not plan:
        print("\nМиграция не нужна — схема уже актуальна.")
        return

    print("\nПлан:")
    for stmt in plan:
        print("  " + stmt)

    if not apply:
        print("\n(пробный режим). Чтобы применить: python tools/migrate_manager_keys.py --apply")
        return

    with engine.begin() as conn:
        for stmt in plan:
            conn.execute(text(stmt))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_manager_kcell_login ON manager (kcell_login);"))
    print(f"\n💾 Применено: {len(plan)} команд (+ индекс на kcell_login).")


if __name__ == "__main__":
    main()
