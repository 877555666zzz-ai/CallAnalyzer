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
import logging
import os
import re
import sys
import threading
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request, Query, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# Без этого INFO/WARNING из фоновой обработки вебхука (см. /webhook/kcell ниже) молча
# резались бы дефолтным root-логгером — в проде это значит "в логах Railway ничего не видно".
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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

# Ни импорт модуля, ни старт uvicorn НЕ должны бить в БД: Railway (Railpack) реально
# выполняет команду запуска приложения прямо во время сборки образа как проверочный шаг,
# а у этого шага нет доступа к приватной сети (postgres.railway.internal резолвится только
# у задеплоенного контейнера) — что startup-событие, что module-level коннект одинаково
# валили сборку. Поэтому Session собирается лениво, при первом реальном запросе.
_session_lock = threading.Lock()
_sessionmaker = None


def Session():
    global _sessionmaker
    if _sessionmaker is None:
        with _session_lock:
            if _sessionmaker is None:
                _sessionmaker = get_sessionmaker(get_engine(DB_URL))
    return _sessionmaker()


app = FastAPI(title="Call Analyzer")
templates = Jinja2Templates(directory=str(ROOT / "dashboard" / "templates"))


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    """Чистый liveness-пробник без похода в БД — для Railway healthcheck/restart policy."""
    return "ok"

_audio_dir = ROOT / "out" / "audio"
_audio_dir.mkdir(parents=True, exist_ok=True)
app.mount("/audio", StaticFiles(directory=str(_audio_dir)), name="audio")


# ---------- Basic-auth: дашборд отдаёт записи разговоров и ПДн, без защиты открывать нельзя (§8.5) ----------
# Логин/пароль из env DASHBOARD_USER / DASHBOARD_PASS. Если не заданы — доступ открыт (только для
# локального дева); в ПРОДЕ задать обязательно. Публичный плеер по подписанной ссылке (/r/{token})
# исключён из проверки — там роль пароля выполняет сам токен с истечением.
import base64 as _b64
import secrets as _secrets
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as _Response

_DASH_USER = os.environ.get("DASHBOARD_USER")
_DASH_PASS = os.environ.get("DASHBOARD_PASS")


class _BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz" or request.url.path.startswith("/r/") \
           or not (_DASH_USER and _DASH_PASS):
            return await call_next(request)  # публичный плеер или auth не настроен (дев)
        header = request.headers.get("Authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                user, _, pw = _b64.b64decode(header[6:]).decode("utf-8").partition(":")
                ok = _secrets.compare_digest(user, _DASH_USER) and _secrets.compare_digest(pw, _DASH_PASS)
            except Exception:
                ok = False
        if not ok:
            return _Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="call-analyzer"'})
        return await call_next(request)


app.add_middleware(_BasicAuthMiddleware)


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


# ---------- Разовая загрузка файла (вне телефонии) ----------
# Тот же путь, что и run_local_audio.py (STT -> analyze_call -> store.save_call_with_analysis),
# только через веб-форму и на один файл за раз. Результат ложится в общий список «Звонки» —
# отдельного шаблона для результата не нужно, call_detail.html показывает его как обычный звонок.
_upload_engines = None
_upload_engines_lock = threading.Lock()
_UPLOAD_MAX_BYTES = 30 * 1024 * 1024  # 30MB — с запасом на длинный звонок в mp3


def _get_upload_engines():
    global _upload_engines
    if _upload_engines is None:
        with _upload_engines_lock:
            if _upload_engines is None:
                from src.runtime import build_llm, build_stt
                llm, _ = build_llm()
                stt, _ = build_stt()
                _upload_engines = (llm, stt)
    return _upload_engines


def _detect_channel(path: Path) -> str:
    try:
        import soundfile as sf
        return "stereo" if sf.info(str(path)).channels >= 2 else "mono"
    except Exception:
        return "stereo"


# Реплики оператора самопредставляются почти всегда ("меня зовут Мария", "это вас Асель
# беспокоит") — проверено сегодня на живых звонках. Ищем известное имя менеджера рядом
# со словом-триггером самопредставления в РЕПЛИКАХ ОПЕРАТОРА (не клиента — иначе поймаем
# случайное упоминание имени в разговоре). Не голосовая биометрика — то, что сказано,
# а не как звучит; для этого не нужна заранее записанная база голосов.
# Сравнение НЕЧЁТКОЕ (difflib, не точный regex): бэктест на 23 живых звонках с реальным
# содержанием поймал ошибку STT "Данна" (логин Kcell) -> "Дана" (расслышал STT) — одна
# буква ломала точное совпадение по границе слова. Порог 0.8 подобран так, чтобы прощать
# такие опечатки STT, но не путать разных менеджеров между собой.
# 0.8 путал реального "Данна" (Kcell) с оператором, представившимся клиенту чужим именем
# "Жанна" (ratio ровно 0.8) — операторы иногда называются не своим именем, это не ловится
# никаким текстовым сравнением, но 0.85 хотя бы не путает РАЗНЫХ менеджеров между собой,
# всё ещё прощая опечатку STT "Данна"->"Дана" (ratio 0.89).
_INTRO_CUE = re.compile(r"(зовут|беспокоит|это\s+вас|это\s+вам)", re.IGNORECASE)
_FUZZY_THRESHOLD = 0.85


def _detect_operator_manager(segments: list[dict], managers: list) -> "Manager | None":
    import difflib
    operator_text = " ".join(s.get("text", "") for s in segments if s.get("speaker") == "operator")
    if not operator_text:
        return None
    words = re.findall(r"[а-яёa-z]+", operator_text.lower())
    candidates = []
    for cue in _INTRO_CUE.finditer(operator_text):
        # окно слов вокруг триггера самопредставления, а не вокруг совпавшего имени —
        # так фуззи-сравнение проверяет только реально релевантные слова, не весь текст
        cue_word_idx = len(re.findall(r"[а-яёa-z]+", operator_text[:cue.start()].lower()))
        window_words = words[max(0, cue_word_idx - 4): cue_word_idx + 4]
        for w in window_words:
            if len(w) < 3:
                continue
            for mgr in managers:
                first_name = mgr.full_name.split()[0].lower()
                ratio = difflib.SequenceMatcher(None, w, first_name).ratio()
                if ratio >= _FUZZY_THRESHOLD:
                    candidates.append((mgr, cue.start(), ratio))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-c[2], c[1]))  # увереннее совпадение, потом раньше по тексту
    return candidates[0][0]


@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, error: str = Query("")):
    return templates.TemplateResponse(request, "upload.html", {"error": error})


@app.post("/upload")
async def upload_submit(audio: UploadFile = File(...), client_number: str = Form(""),
                        direction: str = Form("outbound")):
    import uuid
    from datetime import datetime as _dt
    from urllib.parse import quote
    from src.analyzer import analyze_call
    from src import store

    def _err(msg: str) -> RedirectResponse:
        return RedirectResponse("/upload?error=" + quote(msg), status_code=303)

    body = await audio.read()
    if not body:
        return _err("Пустой файл")
    if len(body) > _UPLOAD_MAX_BYTES:
        return _err("Файл больше 30МБ")

    call_id = f"upload-{uuid.uuid4().hex[:12]}"
    ext = Path(audio.filename or "").suffix or ".mp3"
    dst = _audio_dir / f"{call_id}{ext}"
    dst.write_bytes(body)
    channel = _detect_channel(dst)

    llm, stt = _get_upload_engines()
    try:
        segments = stt.transcribe(str(dst), channel)

        with Session() as s:
            all_managers = s.query(Manager).all()
            mgr = _detect_operator_manager(segments, all_managers)
        if mgr is None:
            dst.unlink(missing_ok=True)
            return _err("Не смог определить оператора автоматически (не назвал имя явно "
                        "в начале звонка) — нужна запись, где оператор представляется")

        mgr_key = mgr.kcell_login or mgr.internal_number
        if not mgr_key:
            dst.unlink(missing_ok=True)
            return _err("У менеджера не задан логин/номер")

        call = {"call_id": call_id, "metadata": {
            "datetime": _dt.now().isoformat(), "direction": direction,
            "operator_login": mgr_key, "client_number": client_number.strip() or "не указан",
            "channel": channel, "audio_url": str(dst),
        }, "segments": segments}
        analysis = analyze_call(call, CFG, llm)
        with Session() as s:
            store.save_call_with_analysis(s, call, analysis)
    except Exception as e:
        log = logging.getLogger("upload")
        log.exception("upload %s: обработка не удалась", call_id)
        return _err(f"Обработка не удалась: {str(e)[:200]}")

    return RedirectResponse(f"/calls/{call_id}", status_code=303)


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


# ---------- Вебхук Kcell: приём звонков push'ем вместо опроса (см. CLAUDE.md, «вторым этапом») ----------
# Настраивается в кабинете Kcell: «Адрес вашей CRM» = https://.../webhook/kcell,
# «Ключ для авторизации» = значение KCELL_CRM_TOKEN. ВАТС шлёт application/x-www-form-urlencoded
# и ждёт быстрый ответ — тяжёлая обработка (скачивание записи, STT, LLM) уходит в BackgroundTasks,
# иначе ВАТС словит таймаут и будет ретраить.
_webhook_log = logging.getLogger("webhook.kcell")
_kcell_pipeline = None
_kcell_pipeline_lock = threading.Lock()


def _get_kcell_pipeline():
    """Ленивая сборка Pipeline для обработки вебхуков — по той же причине, что и Session()
    выше (см. коммент в начале файла): не строить платные клиенты/сеть при импорте модуля."""
    global _kcell_pipeline
    if _kcell_pipeline is None:
        with _kcell_pipeline_lock:
            if _kcell_pipeline is None:
                from src.runtime import build_llm, build_stt
                from src.telephony import get_telephony_client
                from src.bitrix_client import BitrixClient
                from src.pipeline import Pipeline
                llm, _ = build_llm()
                stt, _ = build_stt()
                telephony = get_telephony_client()
                bitrix = BitrixClient(os.environ["BITRIX_WEBHOOK"]) if os.environ.get("BITRIX_WEBHOOK") else None
                _kcell_pipeline = Pipeline(CFG, Session, stt=stt, llm=llm, telephony=telephony, bitrix=bitrix,
                                           dashboard_base=os.environ.get("DASHBOARD_BASE"))
    return _kcell_pipeline


def _handle_kcell_history(row: dict) -> None:
    """Выполняется в фоне (после ответа ВАТС) — один звонок, не валит вебхук при ошибке."""
    call_id = row.get("uid", "?")
    try:
        outcome = _get_kcell_pipeline().process_single(row)
        _webhook_log.info("kcell webhook: call %s -> %s", call_id, outcome)
    except Exception:
        _webhook_log.exception("kcell webhook: обработка звонка %s упала", call_id)


@app.post("/webhook/kcell")
async def kcell_webhook(request: Request, background_tasks: BackgroundTasks):
    form = await request.form()
    data = dict(form)

    expected = os.environ.get("KCELL_CRM_TOKEN")
    if not expected or data.get("crm_token") != expected:
        return JSONResponse({"error": "invalid crm_token"}, status_code=403)

    cmd = data.get("cmd")
    if cmd != "history":
        # event/contact/rating — не обрабатываем на этом этапе, просто подтверждаем приём
        _webhook_log.info("kcell webhook: cmd=%s (без обработки)", cmd)
        return JSONResponse({"result": "ok"})

    background_tasks.add_task(_handle_kcell_history, data)
    return JSONResponse({"result": "ok"})


# ---------- Забор звонков за день: оценка стоимости -> апрув -> запуск в фоне -> история ----------
# Грубая оценка $/звонок (STT + LLM). Не точный биллинг — ориентир перед тем как жать "запустить",
# чтобы не улететь на дневной трафик вслепую. LLM-часть берётся по реально настроенной модели
# (GEMINI_MODEL/ANTHROPIC), STT — усреднённая оценка Deepgram + редкая эскалация на ElevenLabs.
_EST_LLM_COST_PER_CALL_USD = {
    "gemini-2.5-flash-lite": 0.0003, "gemini-2.5-flash": 0.0013, "claude-haiku-4-5": 0.003,
}
_EST_STT_COST_PER_CALL_USD = 0.004

_ingest_jobs: dict[str, dict] = {}
_ingest_jobs_lock = threading.Lock()


def _estimate_cost_usd(n_calls: int) -> float:
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash") if os.environ.get("GEMINI_API_KEY") \
        else os.environ.get("LLM_MODEL", "claude-haiku-4-5")
    llm_cost = _EST_LLM_COST_PER_CALL_USD.get(model, 0.0013)
    return n_calls * (llm_cost + _EST_STT_COST_PER_CALL_USD)


def _run_ingest_job(date_str: str, target: int) -> None:
    from datetime import datetime as _dt
    try:
        d = _dt.strptime(date_str, "%Y-%m-%d").date()
        stats = _get_kcell_pipeline().process_period(d, d)
        with _ingest_jobs_lock:
            _ingest_jobs[date_str] = {"status": "done", "target": target, "stats": stats}
    except Exception as e:
        logging.getLogger("ingest").exception("забор за %s упал", date_str)
        with _ingest_jobs_lock:
            _ingest_jobs[date_str] = {"status": "error", "target": target, "error": str(e)[:300]}


@app.get("/ingest", response_class=HTMLResponse)
def ingest_page(request: Request, date: str = Query(""), error: str = Query("")):
    from datetime import datetime as _dt, date as _date
    from sqlalchemy import func

    estimate = None
    job = _ingest_jobs.get(date) if date else None
    if date and job is None:
        try:
            d = _dt.strptime(date, "%Y-%m-%d").date()
            stats = _get_kcell_pipeline().estimate_period(d, d)
            estimate = {**stats, "cost_usd": round(_estimate_cost_usd(stats["would_process"]), 2)}
        except Exception as e:
            error = f"Не удалось оценить: {str(e)[:200]}"

    # func.date() без явного type_=Date — иначе SQLAlchemy пытается распарсить результат как
    # Python date и падает на SQLite (там нет настоящего DATE-типа, только текст).
    with Session() as s:
        day_col = func.date(Call.started_at).label("day")
        history = s.query(day_col, func.count(Call.id).label("cnt"))\
            .group_by(day_col).order_by(func.max(Call.started_at).desc()).limit(30).all()

    return templates.TemplateResponse(request, "ingest.html", {
        "date": date, "estimate": estimate, "job": job, "error": error,
        "today": _date.today().isoformat(), "history": history,
    })


@app.post("/ingest/run")
def ingest_run(background_tasks: BackgroundTasks, date: str = Form(...), target: int = Form(...)):
    with _ingest_jobs_lock:
        _ingest_jobs[date] = {"status": "running", "target": target}
    background_tasks.add_task(_run_ingest_job, date, target)
    return RedirectResponse(f"/ingest?date={date}", status_code=303)


@app.post("/ingest/reset")
def ingest_reset(date: str = Form(...)):
    """Удаляет все звонки/разборы/транскрипты за дату — для отмены ошибочного забора.
    Разбор денег и остальные отчёты просто перестанут видеть эти звонки."""
    from sqlalchemy import func
    with Session() as s:
        ids = [c.id for c in s.query(Call.id).filter(func.date(Call.started_at) == date).all()]
        for call_id in ids:
            for model in (Analysis, Transcript, Call):
                obj = s.get(model, call_id)
                if obj:
                    s.delete(obj)
        s.commit()
    with _ingest_jobs_lock:
        _ingest_jobs.pop(date, None)
    return RedirectResponse("/ingest", status_code=303)