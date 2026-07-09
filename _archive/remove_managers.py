"""Удаляет из базы менеджеров, не относящихся к продажам. Запускать из КОРНЯ проекта."""
import os
from pathlib import Path
for line in Path(".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
from src.db import get_engine, get_sessionmaker, Manager
Session = get_sessionmaker(get_engine(os.environ.get("DATABASE_URL")))
remove = ["626", "266", "218", "555", "777"]
with Session() as s:
    for num in remove:
        m = s.query(Manager).filter_by(sipuni_internal_number=num).one_or_none()
        if m:
            print("удаляю", num, m.full_name)
            s.delete(m)
    s.commit()
    print("осталось менеджеров:", s.query(Manager).count())
    print("оставшиеся номера:", sorted(x.sipuni_internal_number for x in s.query(Manager).all()))