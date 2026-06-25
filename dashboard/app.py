#!/usr/bin/env python3
"""
Веб-дашборд (§8.1 ТЗ). FastAPI + серверный рендер.
Страницы:
  /                 — «Где деньги» (агрегаты, потери, ред-флаги, по менеджерам)
  /calls            — список звонков с фильтрами (поиск по номеру §8.5)
  /calls/{id}       — drill-down: аудио + транскрипт с подсветкой ред-флагов + разбор
  /api/report/money — JSON отчёта (для интеграций)

Запуск:
  pip install fastapi uvicorn jinja2
  DATABASE_URL=sqlite:///out/demo.db uvicorn dashboard.app:app --reload
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.db import get_engine, get_sessionmaker, Call, Analysis, Transcript, Manager, Recording
from src.report_money import build_money_report
from src.report_conversions import build_stage2
from src.report_tops import tops as build_tops
from src import sharing
from src.analyzer import load_config

CFG = load_config(ROOT / "configs" / "yandex_taxi_corp.yaml")
DB_URL = os.environ.get("DATABASE_URL", "sqlite:///" + str(ROOT / "out" / "demo.db"))
Session = get_sessionmaker(get_engine(DB_URL))

app = FastAPI(title="Call Analyzer")
templates = Jinja2Templates(directory=str(ROOT / "dashboard" / "templates"))

_audio_dir = ROOT / "out" / "audio"
_audio_dir.mkdir(parents=True, exist_ok=True)
app.mount("/audio", StaticFiles(directory=str(_audio_dir)), name="audio")


def _score(analysis: dict) -> int:
    passed = sum(1 for c in analysis["checklist"] if c.get("passed") is True)
    total = sum(1 for c in analysis["checklist"] if c.get("passed") is not None)
    return round(100 * passed / total) if total else 0


@app.get("/", response_class=HTMLResponse)
def money(request: Request):
    with Session() as s:
        report = build_money_report(s, CFG["economics"])
    return templates.TemplateResponse(request, "money.html", {"r": report})


@app.get("/api/report/money", response_class=JSONResponse)
def money_json():
    with Session() as s:
        return build_money_report(s, CFG["economics"])


@app.get("/calls", response_class=HTMLResponse)
def calls(request: Request, phone: str = Query(""), manager: str = Query(""),
          redflags: bool = Query(False)):
    rows = []
    with Session() as s:
        q = s.query(Call, Analysis, Manager).join(Analysis, Analysis.call_id == Call.id)\
            .join(Manager, Manager.id == Call.manager_id).order_by(Call.started_at.desc())
        if phone:
            q = q.filter(Call.client_number.like(f"%{phone}%"))
        if manager:
            q = q.filter(Manager.full_name.like(f"%{manager}%"))
        for call, an, mgr in q.all():
            a = an.data
            if redflags and not a["redflags"]:
                continue
            rows.append({
                "id": call.id, "time": call.started_at.strftime("%d.%m %H:%M"),
                "manager": mgr.full_name, "client": call.client_number,
                "result": a["result_classification"]["primary"],
                "score": _score(a), "redflags": len(a["redflags"]),
                "talk": a["metrics"]["talk_ratio_operator_pct"],
            })
    return templates.TemplateResponse(request, "calls.html", {"rows": rows,
                                      "phone": phone, "manager": manager, "redflags": redflags})


@app.get("/calls/{call_id}", response_class=HTMLResponse)
def call_detail(request: Request, call_id: str):
    with Session() as s:
        call = s.get(Call, call_id)
        an = s.get(Analysis, call_id)
        tr = s.get(Transcript, call_id)
        mgr = s.get(Manager, call.manager_id) if call and call.manager_id else None
    if not call or not an:
        return HTMLResponse("Звонок не найден", status_code=404)

    a = an.data
    quotes = [rf["quote"] for rf in a["redflags"] if rf.get("quote")]
    segments = []
    for seg in (tr.segments if tr else []):
        flagged = any(q and q.lower() in seg["text"].lower() for q in quotes)
        segments.append({**seg, "flagged": flagged})

    audio_name = Path(call.audio_url).name if call.audio_url else None
    audio_exists = bool(audio_name and (_audio_dir / audio_name).exists())
    return templates.TemplateResponse(request, "call_detail.html", {
        "call": call, "mgr": mgr, "a": a, "segments": segments,
        "score": _score(a), "audio_name": audio_name if audio_exists else None,
    })


# ---------- Этап 2: конверсии, тёплые, сверка с CRM ----------
@app.get("/conversions", response_class=HTMLResponse)
def conversions(request: Request):
    with Session() as s:
        data = build_stage2(s, CFG.get("project"))
    return templates.TemplateResponse(request, "conversions.html", {"d": data})


@app.get("/tops", response_class=HTMLResponse)
def tops_page(request: Request):
    with Session() as s:
        t = build_tops(s, CFG.get("project"), n=20)
    return templates.TemplateResponse(request, "tops.html", {"t": t})


# ---------- Ролевые представления (§8.1) ----------
@app.get("/boss", response_class=HTMLResponse)
def boss(request: Request):
    with Session() as s:
        money = build_money_report(s, CFG["economics"])
        st2 = build_stage2(s, CFG.get("project"))
    return templates.TemplateResponse(request, "boss.html", {"r": money, "d": st2})


@app.get("/rop", response_class=HTMLResponse)
def rop(request: Request):
    with Session() as s:
        rows = s.query(Analysis, Manager).join(Call, Call.id == Analysis.call_id)\
            .outerjoin(Manager, Manager.id == Call.manager_id).all()
        t = build_tops(s, CFG.get("project"), n=20)
    # агрегаты соблюдения скрипта по операторам
    agg = {}
    for an, mgr in rows:
        a = an.data
        name = mgr.full_name if mgr else "—"
        d = agg.setdefault(name, {"calls": 0, "greeted": 0, "introduced": 0, "empathy_sum": 0,
                                  "empathy_n": 0, "price_ok": 0, "price_n": 0, "redflags": 0})
        d["calls"] += 1
        by = {c["id"]: c for c in a["checklist"]}
        if by.get("greeted", {}).get("passed"): d["greeted"] += 1
        if by.get("introduced", {}).get("passed"): d["introduced"] += 1
        emp = by.get("empathy", {})
        if emp.get("score") is not None:
            d["empathy_sum"] += emp["score"]; d["empathy_n"] += 1
        price = by.get("handled_price_objection", {})
        if price.get("passed") is not None:
            d["price_n"] += 1
            if price.get("passed"): d["price_ok"] += 1
        d["redflags"] += len(a["redflags"])
    table = []
    for name, d in agg.items():
        table.append({
            "manager": name, "calls": d["calls"],
            "greeted_pct": round(100 * d["greeted"] / d["calls"]) if d["calls"] else 0,
            "introduced_pct": round(100 * d["introduced"] / d["calls"]) if d["calls"] else 0,
            "empathy_avg": round(d["empathy_sum"] / d["empathy_n"]) if d["empathy_n"] else 0,
            "price_pct": round(100 * d["price_ok"] / d["price_n"]) if d["price_n"] else 0,
            "redflags": d["redflags"],
        })
    table.sort(key=lambda r: r["redflags"], reverse=True)
    return templates.TemplateResponse(request, "rop.html", {"table": table, "problematic": t["problematic"]})


# ---------- Шаринг записей: подписанные ссылки + лог доступа (§8.5) ----------
@app.get("/share/{recording_id}", response_class=JSONResponse)
def share(request: Request, recording_id: str, ttl: int = 86400):
    with Session() as s:
        rec = s.get(Recording, recording_id)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        kind = rec.kind
        sharing.log_access(s, recording_id, actor=request.client.host if request.client else "?",
                           action="copy_link", ip=request.client.host if request.client else None)
    base = os.environ.get("DASHBOARD_BASE", str(request.base_url).rstrip("/"))
    return {"url": sharing.share_url(base, recording_id, kind, ttl),
            "kind": kind, "expires_in_sec": ttl,
            "note": "Ссылка не публичная, действует ограниченное время. Доступ логируется."}


@app.get("/r/{token}")
def open_shared(request: Request, token: str):
    info = sharing.verify_token(token)
    if not info:
        return PlainTextResponse("Ссылка недействительна или истекла", status_code=403)
    with Session() as s:
        rec = s.get(Recording, info["recording_id"])
        if not rec:
            return PlainTextResponse("Запись не найдена", status_code=404)
        kind, call_id, object_path = rec.kind, rec.call_id, rec.object_path
        sharing.log_access(s, rec.id, actor=request.client.host if request.client else "?",
                           action="play", ip=request.client.host if request.client else None)
    path = ROOT / object_path
    badge = "<p style='color:#f59e0b'>⚠️ УЧЕБНАЯ / ОТРЕДАКТИРОВАНО</p>" if (kind == "edited") else ""
    audio = (f"<audio controls style='width:100%' src='/audio/{path.name}'></audio>"
             if path.exists() else "<p>Файл записи недоступен в демо.</p>")
    return HTMLResponse(
        f"<div style='font-family:system-ui;max-width:640px;margin:40px auto;color:#e6edf3;background:#1a212b;"
        f"padding:24px;border-radius:12px'>{badge}<h3>Запись {call_id} ({kind})</h3>{audio}"
        f"<p style='color:#8b97a7'>Доступ к этой записи залогирован.</p></div>")
