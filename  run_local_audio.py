#!/usr/bin/env python3
"""Прогон локальных mp3 (out/warm) через route+Gemini -> база. Без Сипуни."""
import os
import sys
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

_env = ROOT / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from src.analyzer import load_config, analyze_call
from src.llm_client import GeminiClient, AnthropicClient, MockClient
from src.stt import get_engine as stt_engine
from src.db import get_engine, get_sessionmaker
from src import store


def build_stt():
    mode = os.environ.get("STT_MODE", "mock")
    kw = {"model_size": os.environ.get("WHISPER_MODEL", "large-v3")}
    if mode == "deepgram":
        kw.update(api_key=os.environ["DEEPGRAM_API_KEY"],
                  model=os.environ.get("DEEPGRAM_MODEL", "nova-3"),
                  language=os.environ.get("DEEPGRAM_LANG", "multi"))
    elif mode == "elevenlabs":
        kw.update(api_key=os.environ["ELEVENLABS_API_KEY"])
    elif mode == "route":
        kw.update(deepgram_key=os.environ["DEEPGRAM_API_KEY"],
                  deepgram_model=os.environ.get("DEEPGRAM_MODEL", "nova-3"),
                  deepgram_language=os.environ.get("DEEPGRAM_LANG", "multi"),
                  elevenlabs_key=os.environ["ELEVENLABS_API_KEY"])
    return stt_engine(mode, **kw), mode


def build_llm():
    if os.environ.get("GEMINI_API_KEY"):
        return GeminiClient(), "Gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicClient(), "Anthropic"
    return MockClient(), "mock"


def main():
    src_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (ROOT / "out" / "warm")
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 12

    cfg = load_config(ROOT / "configs" / "yandex_taxi_corp.yaml")
    Session = get_sessionmaker(get_engine(os.environ.get("DATABASE_URL")))
    stt, stt_mode = build_stt()
    llm, llm_name = build_llm()
    print(f"LLM: {llm_name} | STT: {stt_mode}\n")

    audio_dir = ROOT / "out" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(src_dir.glob("*.mp3"))[:limit]
    if not files:
        print(f"! Нет mp3 в {src_dir}"); return

    with Session() as s:
        managers = sorted(store.managed_internal_numbers(s))
        if not managers:
            print("! Нет менеджеров. Сначала: python3 setup_managers.py --apply"); return
        print(f"Менеджеров: {len(managers)} | файлов: {len(files)}\n")

        ok = err = 0
        for i, f in enumerate(files):
            call_id = f.stem
            internal = managers[i % len(managers)]
            try:
                started = datetime.fromtimestamp(float(call_id.split(".")[0]))
            except Exception:
                started = datetime.now()

            dst = audio_dir / f.name
            if not dst.exists():
                shutil.copy(f, dst)

            print(f"[{i+1}/{len(files)}] {call_id}  оп.{internal} ...", flush=True)
            try:
                segments = stt.transcribe(str(dst), "mono")
                call = {"call_id": call_id, "metadata": {
                    "datetime": started.isoformat(), "direction": "outbound",
                    "operator_internal_number": internal, "client_number": "тест",
                    "channel": "mono", "audio_url": str(dst),
                }, "segments": segments}
                analysis = analyze_call(call, cfg, llm)
                store.save_call_with_analysis(s, call, analysis)
                ok += 1
                print(f"     ok · реплик {len(segments)} · {analysis['result_classification']['primary']}")
            except Exception as e:
                err += 1
                print(f"     ошибка: {str(e)[:120]}")

        print(f"\nГотово. Записано: {ok}, ошибок: {err}")


if __name__ == "__main__":
    main()