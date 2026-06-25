"""
Слой БД (раздел 9 ТЗ). SQLAlchemy → одинаковый код для PostgreSQL (прод) и SQLite (локально).
Прод: DATABASE_URL=postgresql+psycopg://user:pass@host/db
Локально (по умолчанию): SQLite-файл.
JSON-разбор кладём как JSON-тип (в Postgres станет JSONB при желании — см. README).
"""
from __future__ import annotations
import os
from datetime import datetime
from typing import Any

from sqlalchemy import String, Integer, Float, DateTime, ForeignKey, JSON, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class Manager(Base):
    __tablename__ = "manager"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200))
    sipuni_internal_number: Mapped[str] = mapped_column(String(32), index=True)  # маппинг §4.2
    department: Mapped[str] = mapped_column(String(64), index=True)
    project: Mapped[str] = mapped_column(String(64), index=True)


class Call(Base):
    __tablename__ = "call"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # call_id из Сипуни
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    direction: Mapped[str] = mapped_column(String(16))
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("manager.id"), nullable=True, index=True)
    department: Mapped[str] = mapped_column(String(64), index=True)
    project: Mapped[str] = mapped_column(String(64), index=True)
    client_number: Mapped[str] = mapped_column(String(32), index=True)  # поиск по номеру §8.5
    audio_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    channel: Mapped[str] = mapped_column(String(8), default="mono")
    status: Mapped[str] = mapped_column(String(24), default="new", index=True)

    manager: Mapped["Manager | None"] = relationship()
    analysis: Mapped["Analysis | None"] = relationship(back_populates="call", uselist=False)


class Analysis(Base):
    __tablename__ = "analysis"
    call_id: Mapped[str] = mapped_column(ForeignKey("call.id"), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)  # весь разбор по analysis_schema.json
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    call: Mapped["Call"] = relationship(back_populates="analysis")


class Transcript(Base):
    __tablename__ = "transcript"
    call_id: Mapped[str] = mapped_column(ForeignKey("call.id"), primary_key=True)
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSON)  # [{speaker,start,end,text,lang}]


class UnmatchedCall(Base):
    """Звонки без привязки к менеджеру — контроль потерь (§4.2, mapping_unmatched)."""
    __tablename__ = "mapping_unmatched"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sipuni_internal_number: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(String(128))


class Deal(Base):
    """Сделка/лид из Bitrix (§7, §9). Нужна для конверсий, скорости отработки тёплых, сверки с CRM."""
    __tablename__ = "deal"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)          # ID сделки в Bitrix
    client_number: Mapped[str] = mapped_column(String(32), index=True)
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("manager.id"), nullable=True, index=True)
    department: Mapped[str | None] = mapped_column(String(64), index=True)
    project: Mapped[str | None] = mapped_column(String(64), index=True)
    stage: Mapped[str | None] = mapped_column(String(64))                  # стадия в Bitrix (NEW/КП/КДЗ/WON/LOSE...)
    is_warm: Mapped[bool] = mapped_column(default=False)                   # метка «тёплый/прогретый»
    is_legal: Mapped[bool | None] = mapped_column(nullable=True)          # юрлицо(True)/физлицо(False), по версии CRM
    won: Mapped[bool | None] = mapped_column(nullable=True)               # закрыта успешно
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    warm_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)        # попадание в «тёплые»
    first_call_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # первый звонок менеджера
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Recording(Base):
    """Запись разговора: оригинал/учебная (§8.4, §8.5). Оригинал immutable."""
    __tablename__ = "recording"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("call.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="original")     # original | edited
    object_path: Mapped[str] = mapped_column(String(512))
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)  # для edited: «УЧЕБНАЯ»
    immutable: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AccessLog(Base):
    """Лог доступа к записям (§8.5): кто слушал/копировал/выгружал."""
    __tablename__ = "access_log"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    recording_id: Mapped[str] = mapped_column(String(80), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(24))                       # play | copy_link | download
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


def get_engine(url: str | None = None):
    url = url or os.environ.get("DATABASE_URL") or "sqlite:///call_analyzer.db"
    return create_engine(url, future=True)


def get_sessionmaker(engine=None):
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)
