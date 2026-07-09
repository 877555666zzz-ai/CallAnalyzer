#!/usr/bin/env python3
"""
Демо ядра анализа. Транскрипт+метаданные -> структурированный разбор (JSON).

Запуск:
    python run_demo.py                 # авто: Anthropic если есть ANTHROPIC_API_KEY, иначе mock
    python run_demo.py --mock          # принудительно оффлайн-заглушка (без ключей)
    python run_demo.py --gemini        # через Google Gemini (нужен GEMINI_API_KEY + пакет openai)
    python run_demo.py --grok          # через xAI Grok  (нужен XAI_API_KEY + пакет openai)

Ключи берутся из окружения или из файла .env рядом со скриптом (подхватывается автоматически).
"""
import os
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Автозагрузка .env (без внешних зависимостей): не перетираем уже заданные переменные.
_env = ROOT / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from src.analyzer import analyze_call, load_config
from src.llm_client import AnthropicClient, MockClient, GrokClient, GeminiClient


def _pick_client() -> tuple[object, str]:
    if "--grok" in sys.argv:
        c = GrokClient()
        return c, f"Grok [{c.model}]"
    if "--gemini" in sys.argv:
        c = GeminiClient()
        return c, f"Gemini [{c.model}]"
    if "--mock" in sys.argv or not os.environ.get("ANTHROPIC_API_KEY"):
        return MockClient(), "MOCK (оффлайн)"
    c = AnthropicClient()
    return c, f"Anthropic [{c.model}]"


def main() -> None:
    client, mode = _pick_client()
    call = json.loads((ROOT / "sample" / "call_demo_ru_kz.json").read_text(encoding="utf-8"))
    cfg = load_config(ROOT / "configs" / "yandex_taxi_corp.yaml")

    print(f"=== Режим LLM: {mode} ===\n")

    analysis = analyze_call(call, cfg, client)

    out = ROOT / "out" / f"{call['call_id']}.analysis.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(analysis, ensure_ascii=False, indent=2))
    print(f"\n[ok] JSON сохранён: {out}")


if __name__ == "__main__":
    main()