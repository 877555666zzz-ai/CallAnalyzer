#!/usr/bin/env python3
"""
Проверка реальных записей Kcell перед первым боевым прогоном: стерео или моно, кодек,
частота дискретизации, битрейт. В документации API это не указано (§9 задания) —
критично для диаризации: стерео даёт точное разведение оператор/клиент по каналам,
моно — не даёт, и код (DeepgramSTT.diarize=True) на моно работает заметно хуже.

Требует ffprobe (часть ffmpeg) в PATH: brew install ffmpeg / apt install ffmpeg.

Запуск:
    source venv/bin/activate
    python tools/probe_record.py                 # берёт первый попавшийся звонок с записью за вчера
    python tools/probe_record.py --days 3         # искать в последних 3 днях
    python tools/probe_record.py /path/file.mp3   # проверить локальный файл, без похода в Kcell
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_env(path: str = ".env") -> None:
    p = ROOT / path
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def ffprobe(path: Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-show_entries",
          "stream=channels,codec_name,sample_rate,bit_rate", "-of", "json", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe упал: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


def main() -> None:
    load_env()
    if not shutil.which("ffprobe"):
        print("❌ ffprobe не найден в PATH. Установите ffmpeg: brew install ffmpeg (macOS) "
              "или apt install ffmpeg (Linux).")
        return

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    days = 1
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])

    if args:
        local_path = Path(args[0])
        if not local_path.exists():
            print(f"❌ Файл не найден: {local_path}")
            return
        call_id = local_path.stem
        audio_path = local_path
    else:
        from src.kcell_client import KcellClient
        base_url, api_key = os.environ.get("KCELL_BASE_URL"), os.environ.get("KCELL_API_KEY")
        if not base_url or not api_key:
            print("❌ Нет KCELL_BASE_URL / KCELL_API_KEY в .env, и локальный файл не передан.")
            print("   Использование: python tools/probe_record.py /путь/к/записи.mp3")
            return
        c = KcellClient(base_url, api_key)
        ok, msg = c.healthcheck()
        print(f"Kcell healthcheck: {'OK' if ok else 'FAIL'} — {msg}")
        if not ok:
            return

        d_to = date.today()
        d_from = d_to - timedelta(days=days)
        print(f"Ищу звонок с записью за {d_from}..{d_to} ...")
        rows = c.export(d_from, d_to)
        row = next((r for r in rows if KcellClient.map_row(r).get("record_url")), None)
        if row is None:
            print(f"Не нашёл ни одного звонка с записью за последние {days} дн. "
                  "Попробуйте --days больше, или передайте локальный файл.")
            return
        meta = KcellClient.map_row(row)
        call_id = meta["call_id"]
        print(f"Звонок {call_id} ({meta.get('duration_hint_sec')}с) — качаю запись ...")
        audio_bytes = c.download_record(call_id, meta["record_url"])
        audio_path = ROOT / "out" / "audio" / f"{call_id}.mp3"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(audio_bytes)

    info = ffprobe(audio_path)
    streams = info.get("streams") or []
    print(f"\n=== {audio_path.name} ===")
    if not streams:
        print("ffprobe не нашёл аудио-потоков — файл повреждён или не аудио.")
        return
    for i, s in enumerate(streams):
        print(f"поток {i}: channels={s.get('channels')} codec={s.get('codec_name')} "
              f"sample_rate={s.get('sample_rate')} bit_rate={s.get('bit_rate')}")

    channels = streams[0].get("channels")
    print("\n--- вывод ---")
    if channels and int(channels) >= 2:
        print("СТЕРЕО — можно разводить оператор/клиент по каналам, точная диаризация. "
              "default_channel в configs/yandex_taxi_corp.yaml можно оставить 'stereo'.")
    elif channels == 1:
        print("МОНО — точного разведения оператор/клиент по каналам не будет, диаризация "
              "ляжет на DeepgramSTT (diarize=True) + смысловое уточнение ролей в LLM (уже "
              "заложено в src/analyzer.py). Поставьте default_channel: 'mono' в конфиге "
              "и закладывайте в ожидания более шумную диаризацию на части звонков.")
    else:
        print(f"Не удалось однозначно определить число каналов ({channels!r}) — проверьте вручную.")


if __name__ == "__main__":
    main()
