#!/usr/bin/env python3
"""
Демо ядра анализа. Запуск:
    python run_demo.py                 # авто: боевой режим если есть ANTHROPIC_API_KEY, иначе mock
    python run_demo.py --mock          # принудительно оффлайн-заглушка
"""
import os
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from src.analyzer import analyze_call, load_config
from src.llm_client import AnthropicClient, MockClient

ROOT = Path(__file__).resolve().parent


def main() -> None:
    use_mock = "--mock" in sys.argv or not os.environ.get("ANTHROPIC_API_KEY")
    call = json.loads((ROOT / "sample" / "call_demo_ru_kz.json").read_text(encoding="utf-8"))
    cfg = load_config(ROOT / "configs" / "yandex_taxi_corp.yaml")

    client = MockClient() if use_mock else AnthropicClient()
    mode = "MOCK (оффлайн)" if use_mock else f"Anthropic [{client.model}]"
    print(f"=== Режим LLM: {mode} ===\n")

    analysis = analyze_call(call, cfg, client)

    out = ROOT / "out" / f"{call['call_id']}.analysis.json"
    out.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(analysis, ensure_ascii=False, indent=2))
    print(f"\n[ok] JSON сохранён: {out}")


if __name__ == "__main__":
    main()
