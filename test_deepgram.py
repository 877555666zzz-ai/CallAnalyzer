"""
Проверка транскрибации Deepgram на ОДНОМ звонке. Запускать из КОРНЯ проекта.
    python test_deepgram.py sample_call_XXXX.mp3            # моно
    python test_deepgram.py sample_call_XXXX.mp3 stereo     # стерео
"""
import os, sys
from pathlib import Path
for line in Path(".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
from src.stt import DeepgramSTT

def main():
    if len(sys.argv) < 2:
        print("usage: python test_deepgram.py AUDIO_PATH [mono|stereo]")
        return
    audio = sys.argv[1]
    channel = sys.argv[2] if len(sys.argv) > 2 else "mono"

    key = os.environ.get("DEEPGRAM_API_KEY")
    if not key:
        print("! Нет DEEPGRAM_API_KEY в .env"); return
    if not Path(audio).exists():
        print(f"! Файл не найден: {audio}"); return

    stt = DeepgramSTT(key,
                      model=os.environ.get("DEEPGRAM_MODEL", "nova-3"),
                      language=os.environ.get("DEEPGRAM_LANG", "ru"))
    print(f"Транскрибирую ({channel}, model={stt.model}, lang={stt.language}): {audio}")
    print()
    segs = stt.transcribe(audio, channel=channel)

    if not segs:
        print("Пусто — Deepgram ничего не вернул. Проверь формат файла/язык.")
        return

    for s in segs:
        if s["speaker"] == "operator":
            who = "ОПЕРАТОР"
        elif s["speaker"] == "client":
            who = "КЛИЕНТ"
        else:
            who = "?"
        print(f"[{s['start']:>6.1f}-{s['end']:>6.1f}] {who:<8} ({s['lang']}): {s['text']}")

    print()
    print(f"Всего реплик: {len(segs)}")
    print("Проверь: 1) текст читаемый на КЗ/РУ?  2) роли не перепутаны?")

if __name__ == "__main__":
    main()