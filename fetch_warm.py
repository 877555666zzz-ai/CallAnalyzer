#!/usr/bin/env python3
import os, sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for _line in (ROOT / ".env").read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

from src.sipuni_client import SipuniClient
from src.bitrix_client import BitrixClient
from src.bitrix_sync import warm_phones, _digits


def is_mp3(b):
    return bool(b) and len(b) > 2000 and (b[:3] == b"ID3" or (b[0] == 0xFF and (b[1] & 0xE0) == 0xE0))


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    b = BitrixClient(os.environ["BITRIX_WEBHOOK"])
    print("Pulling warm phones from Bitrix ...", flush=True)
    warm = warm_phones(b)
    print("  warm phones:", len(warm))
    sip = SipuniClient(os.environ["SIPUNI_USER"], os.environ["SIPUNI_SECRET"])
    d_to = date.today()
    d_from = d_to - timedelta(days=days)
    print("Pulling calls for", days, "days (%s-%s) ..." % (d_from.strftime("%d.%m"), d_to.strftime("%d.%m")), flush=True)
    rows = sip.export(d_from, d_to)
    warm_calls = []
    for row in rows:
        m = SipuniClient.map_row(row)
        cn = _digits(m.get("client_number"))
        dur = m.get("duration_hint_sec") or 0
        if len(cn) >= 10 and cn[-10:] in warm and dur >= 15 and m.get("call_id"):
            warm_calls.append(m)
    print("  warm calls with duration >=15s:", len(warm_calls), "\n")
    outdir = ROOT / "out" / "warm"
    outdir.mkdir(parents=True, exist_ok=True)
    saved = []
    for m in warm_calls:
        if len(saved) >= limit:
            break
        cid = m["call_id"]
        try:
            audio = sip.download_record(cid)
        except Exception as e:
            print("  %s: download failed (%s)" % (cid, e))
            continue
        if not is_mp3(audio):
            print("  %s: no recording, skip" % cid)
            continue
        (outdir / (cid + ".mp3")).write_bytes(audio)
        dur = int(m.get("duration_hint_sec") or 0)
        saved.append(cid)
        print("  [ok] %s  client %s  op.%s  %ss" % (cid, m.get("client_number"), m.get("operator_internal_number"), dur))
    print("\n" + "=" * 64)
    print("Saved recordings:", len(saved), " ->  out/warm/")
    if saved:
        print("\nListen:  open out/warm/%s.mp3" % saved[0])
        print("KZ test: DEEPGRAM_MODEL=whisper-large DEEPGRAM_LANG=kk python test_deepgram.py out/warm/%s.mp3 stereo" % saved[0])


if __name__ == "__main__":
    main()