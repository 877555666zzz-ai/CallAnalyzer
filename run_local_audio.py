#!/usr/bin/env python3
"""
Локальный прогон реальных mp3 (папка out/warm/ по умолчанию) через STT+LLM без
Сипуни-автозабора и без Bitrix-синка — для проверки качества разбора на боевых записях.

Запуск:
    source venv/bin/activate
    python run_local_audio.py [папка] [лимит] [пауза_сек]
    python run_local_audio.py out/warm 5 20
"""
import os
import sys
import shutil
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

_env = ROOT / ".env"
if _env.exists():
    for _l in _env.read_text(encoding="utf-8").splitlines():
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _v = _l.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from src.analyzer import load_config, analyze_call
from src.runtime import build_llm, build_stt
from src.db import get_engine, get_sessionmaker
from src import store


def _detect_channel(path: Path) -> str:
    """Сипуни пишет стерео (оператор/клиент по отдельным каналам) — это и даёт настоящую
    диаризацию. Раньше здесь было жёстко "mono", из-за чего Deepgram вместо разделения по
    каналам пытался угадывать спикеров внутри смешанной дорожки и валил всё в одного
    говорящего. Определяем реальное число каналов файла; если не вышло — стерео как
    безопасный дефолт (соответствует src/pipeline.py: cfg.get("default_channel", "stereo"))."""
    try:
        import soundfile as sf
        return "stereo" if sf.info(str(path)).channels >= 2 else "mono"
    except Exception:
        return "stereo"


def main():
    src_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "out" / "warm"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    delay = int(sys.argv[3]) if len(sys.argv) > 3 else 20

    cfg = load_config(ROOT / "configs" / "yandex_taxi_corp.yaml")
    Session = get_sessionmaker(get_engine(os.environ.get("DATABASE_URL")))
    stt, stt_name = build_stt()
    llm, llm_name = build_llm()
    print("LLM:", llm_name, "| STT:", stt_name, "| pause", delay, "s\n")

    audio_dir = ROOT / "out" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(src_dir.glob("*.mp3"))[:limit]
    if not files:
        print("no mp3 in", src_dir)
        return

    with Session() as s:
        managers = sorted(store.managed_keys(s))
        if not managers:
            print("no managers")
            return
        ok = err = 0
        for i, f in enumerate(files):
            call_id = f.stem
            internal = managers[i % len(managers)]
            try:
                started = datetime.fromtimestamp(float(call_id.split(".")[0]))
            except (ValueError, IndexError):
                started = datetime.now()
            dst = audio_dir / f.name
            if not dst.exists():
                shutil.copy(f, dst)
            channel = _detect_channel(dst)
            print(f"[{i + 1}/{len(files)}] {call_id} op.{internal} ({channel}) ...", flush=True)
            try:
                segments = stt.transcribe(str(dst), channel)
                call = {"call_id": call_id, "metadata": {
                    "datetime": started.isoformat(), "direction": "outbound",
                    "operator_internal_number": internal, "client_number": "test",
                    "channel": channel, "audio_url": str(dst),
                }, "segments": segments}
                analysis = analyze_call(call, cfg, llm)
                store.save_call_with_analysis(s, call, analysis)
                ok += 1
                print("   ok", len(segments), analysis["result_classification"]["primary"])
            except Exception as e:
                err += 1
                print("   err:", str(e)[:200])
            if i < len(files) - 1:
                print(f"   ...pause {delay}s...", flush=True)
                time.sleep(delay)
        print("\nDONE ok=", ok, "err=", err)


if __name__ == "__main__":
    main()
