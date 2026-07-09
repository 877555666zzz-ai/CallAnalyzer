#!/usr/bin/env python3
"""
Тест ElevenLabs Scribe на одном звонке.
    python3 test_elevenlabs.py out/warm/1783507127.433202.mp3
Ключ берётся из .env (ELEVENLABS_API_KEY).
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

for _line in (ROOT / ".env").read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

from elevenlabs_stt import ElevenLabsSTT  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print("Использование: python3 test_elevenlabs.py <путь_к_mp3>")
        sys.exit(1)
    path = sys.argv[1]
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        print("! Нет ELEVENLABS_API_KEY в .env")
        sys.exit(1)
    if not Path(path).exists():
        print(f"! Файл не найден: {path}")
        sys.exit(1)

    print(f"Транскрибирую через ElevenLabs Scribe: {path}")
    eng = ElevenLabsSTT(key)
    segs = eng.transcribe(path)
    for s in segs:
        role = "ОПЕРАТОР" if s["speaker"] == "operator" else ("КЛИЕНТ  " if s["speaker"] == "client" else "?       ")
        print(f"[{s['start']:6.1f}-{s['end']:6.1f}] {role} ({s['lang']}): {s['text']}")
    print(f"Всего реплик: {len(segs)}")
    print("Проверь: 1) текст читаемый на КЗ/РУ?  2) роли не перепутаны?")


if __name__ == "__main__":
    main()