"""
Идемпотентные ALTER TABLE поверх Base.metadata.create_all() — в проекте нет Alembic, а
create_all не трогает уже существующие таблицы, если модель поменялась (см. CLAUDE.md).
Вызывается автоматически из src.db.get_sessionmaker(), поэтому боевая БД самовосстанавливается
при следующем подключении без ручного доступа (важно: ручной доступ к Postgres на Railway
для меня недоступен — личный SSH-ключ пользователя защищён паролем). Та же логика доступна
отдельно как tools/migrate_manager_keys.py для явного просмотра плана/прогона.

Операции только аддитивные/переименование, никогда не дропают данные — безопасно гонять
на каждом старте приложения.
"""
from __future__ import annotations
import logging

log = logging.getLogger("schema_migrate")


def plan_manager_keys(engine) -> list[str]:
    """Считает, какие ALTER TABLE нужны, ничего не меняет. Используется и автопрогоном,
    и tools/migrate_manager_keys.py (там — для дефолтного dry-run режима)."""
    from sqlalchemy import inspect

    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    plan: list[str] = []

    for table, old, new in [
        ("manager", "sipuni_internal_number", "internal_number"),
        ("mapping_unmatched", "sipuni_internal_number", "internal_number"),
    ]:
        if table not in existing_tables:
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if old in cols and new not in cols:
            plan.append(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new};")

    if "manager" in existing_tables:
        cols = {c["name"] for c in insp.get_columns("manager")}
        if "kcell_login" not in cols:
            plan.append("ALTER TABLE manager ADD COLUMN kcell_login VARCHAR(64);")

    return plan


def ensure_manager_keys(engine) -> list[str]:
    """manager.sipuni_internal_number -> internal_number (+ добавляет kcell_login),
    mapping_unmatched.sipuni_internal_number -> internal_number.
    Возвращает список применённых команд (пустой список — миграция не требовалась)."""
    from sqlalchemy import text

    plan = plan_manager_keys(engine)
    if not plan:
        return []

    with engine.begin() as conn:
        for stmt in plan:
            conn.execute(text(stmt))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_manager_kcell_login ON manager (kcell_login);"))
    log.warning("schema_migrate: применено %d команд автоматически: %s", len(plan), plan)
    return plan
