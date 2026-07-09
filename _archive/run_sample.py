import os, sys, time
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

def load_env(path=".env"):
    p = Path(path)
    if not p.exists(): return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

def main():
    load_env()
    if len(sys.argv) < 2:
        print("usage: python3 run_sample.py DD.MM.YYYY [кол-во]"); return
    day = datetime.strptime(sys.argv[1], "%d.%m.%Y").date()
    n = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("N_CALLS", "8"))
    min_dur = float(os.environ.get("MIN_DURATION", "20"))
    model = os.environ.get("WHISPER_MODEL", "medium")
    from src.analyzer import analyze_call, load_config
    from src.llm_client import AnthropicClient, MockClient
    from src.sipuni_client import SipuniClient
    from src.db import get_engine, get_sessionmaker
    from src import store
    cfg = load_config(ROOT / "configs" / "yandex_taxi_corp.yaml")
    Session = get_sessionmaker(get_engine(os.environ.get("DATABASE_URL")))
    sip = SipuniClient(os.environ["SIPUNI_USER"], os.environ["SIPUNI_SECRET"])
    llm = AnthropicClient() if os.environ.get("ANTHROPIC_API_KEY") else MockClient()
    print("LLM:", "Anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "MOCK", "| Whisper:", model)
    print("Тяну звонки за", day.strftime("%d.%m.%Y"), "...")
    rows = sip.export(day, day)
    print("Всего за день:", len(rows))
    with Session() as s:
        picked = []
        for r in rows:
            m = SipuniClient.map_row(r)
            status = (m.get("status") or "").lower()
            if m["direction"] != "outbound" or "не отвеч" in status: continue
            if not m["call_id"] or (m["duration_hint_sec"] or 0) < min_dur: continue
            if not store.find_manager(s, m["operator_internal_number"] or ""): continue
            picked.append(m)
            if len(picked) >= n: break
    print("Отобрано:", len(picked), "\n")
    if not picked:
        print("Нет подходящих звонков. Попробуйте другой день/MIN_DURATION."); return
    print("Гружу Whisper (первый раз — скачивание модели)...")
    from src.stt import WhisperSTT
    stt = WhisperSTT(model_size=model, compute_type="int8")
    audio_dir = ROOT / "out" / "audio"; audio_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    with Session() as s:
        for i, m in enumerate(picked, 1):
            cid = m["call_id"]; t0 = time.time()
            try:
                print("[%d/%d] %s (оп.%s)... " % (i, len(picked), cid, m["operator_internal_number"]), end="", flush=True)
                path = audio_dir / (cid + ".mp3"); path.write_bytes(sip.download_record(cid))
                segments = stt.transcribe(str(path), channel="stereo")
                if not segments: print("пустой транскрипт"); continue
                mgr = store.find_manager(s, m["operator_internal_number"])
                call = {"call_id": cid, "metadata": {
                    "datetime": m["datetime"] or datetime.now().isoformat(),
                    "direction": m["direction"], "operator_internal_number": m["operator_internal_number"],
                    "operator_name": mgr.full_name, "department": mgr.department, "project": mgr.project,
                    "client_number": m["client_number"], "channel": "auto", "audio_url": str(path)},
                    "segments": segments}
                analysis = analyze_call(call, cfg, llm)
                store.save_call_with_analysis(s, call, analysis); ok += 1
                spk = "каналы" if any(x["speaker"] in ("operator","client") for x in segments) else "моно"
                print("ок · %d реплик · %s · %s · %.0fс" % (len(segments), spk, analysis["result_classification"]["primary"], time.time()-t0))
            except Exception as e:
                print("ошибка:", e)
    print("\nГотово: %d/%d. Открывайте дашборд." % (ok, len(picked)))

if __name__ == "__main__":
    main()