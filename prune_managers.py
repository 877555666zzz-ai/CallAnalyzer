#!/usr/bin/env python3
"""
Убрать из базы менеджеров, которые НЕ продажники (по internal_number — легаси Sipuni).
Список EXCLUDE ниже — правь под себя (бухгалтеры, РОП, служебные и т.п.).
После перехода на Kcell фильтрация служебных учёток делается через роль (role="user")
в setup_managers.py — этот скрипт актуален только для менеджеров, заведённых ещё с Sipuni.

    python3 prune_managers.py          # показать, кого уберёт (ничего не пишет)
    python3 prune_managers.py --apply  # реально удалить из базы

Учитывает .env (DATABASE_URL). Идемпотентно.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Внутренние номера Сипуни, которые НЕ должны быть в анализе продаж:
EXCLUDE = {
    "626",  # Тамер и Ильяс
    "266",  # Асель нов каз бухх (бухгалтер)
    "218",  # Зафар
    "555",  # Анара роп (руководитель)
    "777",  # Жасулан
}


def load_env(path=".env"):
    p = ROOT / path
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main():
    load_env()
    apply = "--apply" in sys.argv

    from src.db import get_engine, get_sessionmaker, Manager
    Session = get_sessionmaker(get_engine(os.environ.get("DATABASE_URL")))
    with Session() as s:
        rows = s.query(Manager).filter(Manager.internal_number.in_(EXCLUDE)).all()
        if not rows:
            print("В базе нет менеджеров из списка EXCLUDE — чисто.")
            return
        print(f"{'УДАЛЯЮ' if apply else 'Будут удалены'} ({len(rows)}):")
        for m in rows:
            print(f"   {m.internal_number:>4}  {m.full_name}")
        if not apply:
            print("\n(пробный режим). Чтобы удалить: python3 prune_managers.py --apply")
            return
        for m in rows:
            s.delete(m)
        s.commit()
        print(f"\n💾 Удалено: {len(rows)}. Осталось менеджеров: {s.query(Manager).count()}")


if __name__ == "__main__":
    main()