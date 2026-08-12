#!/usr/bin/env python3
"""
Маппинг менеджеров: тянет сотрудников из Kcell (/users) и заносит в базу (таблица manager),
отсекая служебные/нерабочие учётки по роли и по имени. Идемпотентно — можно запускать повторно.

Запуск:
    source venv/bin/activate
    python setup_managers.py                 # показать, кого добавит (ничего не пишет)
    python setup_managers.py --apply         # реально записать в базу

Учитывает .env (KCELL_BASE_URL/KCELL_API_KEY, DATABASE_URL). По умолчанию проект — yandex_taxi_corp.

Легаси: для менеджеров, ещё заведённых со времён Sipuni (internal_number без kcell_login),
этот скрипт ничего не трогает — используй prune_managers.py, если нужно почистить их вручную.
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def load_env(path=".env"):
    p = ROOT / path
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# Дополнительный фильтр по имени — на случай, если роль в Kcell не проставлена аккуратно
# (боты, тестовые учётки, «не трогать» и т.п.). Роль "user" уже отсекает большинство служебных.
SKIP_PATTERNS = [
    r"бот", r"voice\s*bot", r"звонобот", r"воктив", r"\bтест\b",
    r"своб", r"inbound", r"не\s*трог", r"новый сотрудник",
]
SKIP_RE = re.compile("|".join(SKIP_PATTERNS), re.IGNORECASE)

DEPARTMENT = os.environ.get("DEFAULT_DEPARTMENT", "sales")
PROJECT = os.environ.get("DEFAULT_PROJECT", "yandex_taxi_corp")


def is_real_operator(login: str, name: str, role: str) -> bool:
    if not login:
        return False
    if role not in ("user", "group_head"):  # admin/restricted_user — не продажники
        return False
    if not name or SKIP_RE.search(name):
        return False
    return True


def main():
    load_env()
    apply = "--apply" in sys.argv
    base_url, api_key = os.environ.get("KCELL_BASE_URL"), os.environ.get("KCELL_API_KEY")
    if not base_url or not api_key:
        print("❌ Нет KCELL_BASE_URL / KCELL_API_KEY в .env"); return

    from src.kcell_client import KcellClient
    c = KcellClient(base_url, api_key)
    ok, msg = c.healthcheck()
    print(f"Kcell healthcheck: {'OK' if ok else 'FAIL'} — {msg}")
    if not ok:
        print("Прерываю — ключ/домен не работают."); return

    users = c.operators()

    keep, skip = [], []
    for u in users:
        login = (u.get("login") or "").strip()
        name = (u.get("name") or "").strip()
        role = (u.get("role") or "").strip()
        ext = (u.get("ext") or "").strip() or None
        (keep if is_real_operator(login, name, role) else skip).append((login, name, role, ext))

    print(f"Всего сотрудников в Kcell: {len(users)}")
    print(f"\n✅ Берём как менеджеров ({len(keep)}):")
    for login, name, role, ext in sorted(keep, key=lambda x: x[0]):
        print(f"   {login:<16}  {name:<28} role={role:<10} ext={ext}")
    print(f"\n⏭  Пропускаем (роль/боты/служебные) ({len(skip)}):")
    for login, name, role, ext in sorted(skip, key=lambda x: x[0]):
        print(f"   {login:<16}  {name:<28} role={role}")

    if not apply:
        print("\n(пробный режим — в базу ничего не записано)")
        print("Если список верный, запустите: python setup_managers.py --apply")
        return

    from src.db import get_engine, get_sessionmaker
    from src.store import upsert_manager
    Session = get_sessionmaker(get_engine(os.environ.get("DATABASE_URL")))
    with Session() as s:
        for login, name, role, ext in keep:
            upsert_manager(s, full_name=name, department=DEPARTMENT, project=PROJECT,
                           kcell_login=login, internal_number=ext)
        s.commit()
    print(f"\n💾 Записано менеджеров в базу: {len(keep)} (department={DEPARTMENT}, project={PROJECT})")


if __name__ == "__main__":
    main()
