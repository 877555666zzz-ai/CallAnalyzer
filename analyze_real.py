#!/usr/bin/env python3
"""
Полная цепочка на РЕАЛЬНОМ звонке: mp3 -> Deepgram (транскрипт) -> Gemini (анализ).
Запуск из КОРНЯ:
    python analyze_real.py sample_call_1782884206.326217.mp3
    python analyze_real.py sample_call_XXXX.mp3 --grok     # через Grok вместо Gemini
"""
import os, sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# грузим .env сами
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from src.stt import DeepgramSTT
from src.analyzer import analyze_call, load_config
from src.llm_client import GeminiClient, GrokClient, MockClient


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: python analyze_real.py AUDIO.mp3 [--grok|--mock]")
        return
    audio = args[0]
    if not Path(audio).exists():
        print(f"! Файл не найден: {audio}"); return

    # 1) Транскрибация через Deepgram
    key = os.environ.get("DEEPGRAM_API_KEY")
    if not key:
        print("! Нет DEEPGRAM_API_KEY"); return
    stt = DeepgramSTT(key,
                      model=os.environ.get("DEEPGRAM_MODEL", "nova-3"),
                      language=os.environ.get("DEEPGRAM_LANG", "ru"))
    print(f"[1/2] Транскрибирую {audio} через Deepgram ...")
    segments = stt.transcribe(audio, channel="mono")
    print(f"      Реплик получено: {len(segments)}")
    if not segments:
        print("! Пустой транскрипт"); return

    # упаковываем звонок в структуру, которую ждёт analyze_call
    call = {
        "call_id": Path(audio).stem,
        "metadata": {
            "direction": "outbound",
            "operator_internal_number": "unknown",
            "operator_name": "unknown",
            "department": "sales_taxi",
            "project": "yandex_taxi_corp",
            "client_number": "unknown",
            "channel": "mono",
        },
        "segments": segments,
    }

    # 2) Анализ через LLM
    if "--grok" in sys.argv:
        client = GrokClient(); name = "Grok"
    elif "--mock" in sys.argv:
        client = MockClient(); name = "MOCK"
    else:
        client = GeminiClient(); name = f"Gemini [{os.environ.get('GEMINI_MODEL','gemini-2.5-flash')}]"
    print(f"[2/2] Анализирую через {name} ...\n")

    cfg = load_config(ROOT / "configs" / "yandex_taxi_corp.yaml")
    analysis = analyze_call(call, cfg, client)

    out = ROOT / "out" / (call["call_id"] + ".real.analysis.json")
    out.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")

    # печатаем главное человекочитаемо
    print("=" * 60)
    print("РАЗБОР ЗВОНКА:")
    print("=" * 60)
    print("\nИТОГ:", analysis.get("summary", "-"))
    print("\nЭТАП ПОТЕРИ:", analysis.get("loss_stage", "-"))
    rc = analysis.get("result_classification", {})
    print("КЛАССИФИКАЦИЯ:", rc.get("primary", "-"), "| увер.:", rc.get("confidence", "-"))
    print("\nЧЕКЛИСТ:")
    for c in analysis.get("checklist", []):
        mark = "✅" if c.get("passed") else "❌"
        print(f"  {mark} {c.get('label')}: {c.get('evidence','')[:80]}")
    rf = analysis.get("redflags", [])
    print(f"\nКРАСНЫЕ ФЛАГИ: {len(rf)}")
    for r in rf:
        print(f"  ⚠️  [{r.get('severity')}] {r.get('explanation','')}")
    print(f"\n[ok] Полный JSON: {out}")


if __name__ == "__main__":
    main()