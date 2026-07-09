#!/usr/bin/env python3
"""
Маппинг менеджеров: тянет операторов из Сипуни и заносит в базу (таблица manager),
автоматически отсекая ботов и пустые слоты. Идемпотентно — можно запускать повторно.

Запуск:
    source venv/bin/activate
    python setup_managers.py                 # показать, кого добавит (ничего не пишет)
    python setup_managers.py --apply         # реально записать в базу

Учитывает .env (SIPUNI_USER/SECRET, DATABASE_URL). По умолчанию проект — yandex_taxi_corp.
"""
import os
import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_env(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# Кого НЕ берём: боты, тестовые, пустые слоты, служебные «не трогать».
SKIP_PATTERNS = [
    r"бот", r"voice\s*bot", r"звонобот", r"воктив", r"\bтест\b",
    r"своб", r"inbound", r"не\s*трог", r"новый сотрудник",
]
SKIP_RE = re.compile("|".join(SKIP_PATTERNS), re.IGNORECASE)

# Не продажники (бухгалтеры, РОП, служебные) — отсекаем по внутреннему номеру.
SKIP_LOGINS = {"626", "266", "218", "555", "777"}

DEPARTMENT = os.environ.get("DEFAULT_DEPARTMENT", "sales")
PROJECT = os.environ.get("DEFAULT_PROJECT", "yandex_taxi_corp")


def is_real_operator(login: str, name: str) -> bool:
    if login in SKIP_LOGINS:
        return False
    if not login or not login.isdigit():
        return False
    if not name or SKIP_RE.search(name):
        return False
    return True


def main():
    load_env()
    apply = "--apply" in sys.argv
    user, secret = os.environ.get("SIPUNI_USER"), os.environ.get("SIPUNI_SECRET")
    if not user or not secret:
        print("❌ Нет SIPUNI_USER / SIPUNI_SECRET в .env"); return

    from src.sipuni_client import SipuniClient
    c = SipuniClient(user, secret)
    ops = c.operators()

    keep, skip = [], []
    for r in ops:
        login = (r.get("Login") or "").strip()
        name = (r.get("Name") or "").strip()
        (keep if is_real_operator(login, name) else skip).append((login, name))

    print(f"Всего операторов в Сипуни: {len(ops)}")
    print(f"\n✅ Берём как менеджеров ({len(keep)}):")
    for login, name in sorted(keep, key=lambda x: x[0]):
        print(f"   {login:>4}  {name}")
    print(f"\n⏭  Пропускаем боты/пустые/служебные ({len(skip)}):")
    for login, name in sorted(skip, key=lambda x: x[0]):
        print(f"   {login:>4}  {name}")

    if not apply:
        print("\n(пробный режим — в базу ничего не записано)")
        print("Если список верный, запустите: python setup_managers.py --apply")
        return

    from src.db import get_engine, get_sessionmaker
    from src.store import upsert_manager
    Session = get_sessionmaker(get_engine(os.environ.get("DATABASE_URL")))
    with Session() as s:
        for login, name in keep:
            upsert_manager(s, full_name=name, internal_number=login,
                           department=DEPARTMENT, project=PROJECT)
        s.commit()
    print(f"\n💾 Записано менеджеров в базу: {len(keep)} (department={DEPARTMENT}, project={PROJECT})")


if __name__ == "__main__":
    main()