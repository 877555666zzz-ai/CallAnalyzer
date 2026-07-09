"""
ElevenLabs Scribe STT — адаптер под тот же контракт, что DeepgramSTT:
    transcribe(audio_path, channel="mono") -> list[{speaker, start, end, text, lang}]

Один POST на https://api.elevenlabs.io/v1/speech-to-text (модель scribe_v2),
диаризация + роли agent/customer. Отдельный файл, src/stt.py не трогаем.
"""
from __future__ import annotations

import os
from typing import Any

import requests

ELEVEN_URL = "https://api.elevenlabs.io/v1/speech-to-text"


class ElevenLabsSTT:
    def __init__(self, api_key: str, model: str = "scribe_v2", timeout_sec: int = 300):
        self.api_key = api_key
        self.model = model
        self.timeout_sec = timeout_sec

    def transcribe(self, audio_path: str, channel: str = "mono") -> list[dict[str, Any]]:
        # channel не используется: Scribe делает собственную диаризацию/роли
        with open(audio_path, "rb") as f:
            resp = requests.post(
                ELEVEN_URL,
                headers={"xi-api-key": self.api_key},
                files={"file": f},
                data={
                    "model_id": self.model,
                    "language_code": "kaz",
                    "diarize": "true",
                    "detect_speaker_roles": "true",
                    "timestamps_granularity": "word",
                },
                timeout=self.timeout_sec,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"ElevenLabs {resp.status_code}: {resp.text[:300]}")
        return self._to_segments(resp.json())

    def _to_segments(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        lang = (data.get("language_code") or "auto")
        words = data.get("words") or []

        role_map: dict[Any, str] = {}

        def role_for(spk: Any) -> str:
            # Scribe с detect_speaker_roles может вернуть 'agent'/'customer' прямо в speaker_id
            s = str(spk or "").lower()
            if "agent" in s:
                return "operator"
            if "customer" in s or "client" in s:
                return "client"
            if spk not in role_map:
                role_map[spk] = "operator" if not role_map else ("client" if len(role_map) == 1 else "unknown")
            return role_map[spk]

        segs: list[dict[str, Any]] = []
        cur: dict[str, Any] | None = None
        for w in words:
            if w.get("type") not in (None, "word", "spacing"):
                continue
            txt = w.get("text") or ""
            spk = w.get("speaker_id", "speaker_0")
            start = float(w.get("start", 0) or 0)
            end = float(w.get("end", 0) or 0)
            is_space = (w.get("type") == "spacing") or (not txt.strip())
            if is_space:
                if cur is not None:
                    cur["_buf"] += txt
                    cur["end"] = end
                continue
            if cur is None or spk != cur["spk"]:
                if cur is not None:
                    segs.append(self._flush(cur, lang))
                cur = {"spk": spk, "start": start, "end": end, "_buf": txt}
            else:
                cur["end"] = end
                cur["_buf"] += txt
        if cur is not None:
            segs.append(self._flush(cur, lang))

        for sg in segs:
            sg["speaker"] = role_for(sg.pop("spk"))
        return segs

    @staticmethod
    def _flush(cur: dict[str, Any], lang: str) -> dict[str, Any]:
        return {
            "spk": cur["spk"],
            "start": round(cur["start"], 2),
            "end": round(cur["end"], 2),
            "text": cur["_buf"].strip(),
            "lang": lang,
        }