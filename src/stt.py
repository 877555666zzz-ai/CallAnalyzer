"""
Адаптер транскрипции (§5 ТЗ). Интерфейс единый, движок заменяемый.
Выход везде одинаковый: list[{speaker, start, end, text, lang}] — вход для анализатора.

Дефолт — Whisper (self-host, без счетов). Для стерео-записи каналы разделяются
(оператор/клиент по каналам) — это и есть точная диаризация из §4.4.
Финальный выбор движка — по бейк-офу на ваших боевых звонках (см. README, бенчмарк).
"""
from __future__ import annotations
from typing import Any, Protocol


class STTEngine(Protocol):
    def transcribe(self, audio_path: str, channel: str = "mono") -> list[dict[str, Any]]: ...


class MockSTT:
    """Заглушка для прогона пайплайна без модели/аудио."""
    def __init__(self, segments: list[dict[str, Any]] | None = None):
        self._segments = segments or []

    def transcribe(self, audio_path: str, channel: str = "mono") -> list[dict[str, Any]]:
        return list(self._segments)


class WhisperSTT:
    """
    Self-host транскрипция через faster-whisper. Автоопределение языка (RU/KZ/mixed).
    Стерео: канал 0 -> оператор, канал 1 -> клиент (уточнить раскладку каналов у Сипуни),
    каждый канал распознаётся отдельно и сегменты сливаются по времени — чистая диаризация.
    """
    def __init__(self, model_size: str = "large-v3", device: str = "auto", compute_type: str = "auto"):
        from faster_whisper import WhisperModel  # ставится отдельно: pip install faster-whisper
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def _transcribe_stream(self, audio: Any, sr: int, speaker: str) -> list[dict[str, Any]]:
        import tempfile, soundfile as sf, os
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            sf.write(tmp.name, audio, sr)
            path = tmp.name
        try:
            segments, _info = self.model.transcribe(path, language=None, vad_filter=True, word_timestamps=False)
            out = []
            for s in segments:
                out.append({"speaker": speaker, "start": round(s.start, 2), "end": round(s.end, 2),
                            "text": s.text.strip(), "lang": getattr(s, "language", None) or "auto"})
            return out
        finally:
            os.unlink(path)

    def transcribe(self, audio_path: str, channel: str = "mono") -> list[dict[str, Any]]:
        import soundfile as sf
        audio, sr = sf.read(audio_path, always_2d=True)  # shape (n, channels)
        if channel == "stereo" and audio.shape[1] >= 2:
            op = self._transcribe_stream(audio[:, 0], sr, "operator")
            cl = self._transcribe_stream(audio[:, 1], sr, "client")
            return sorted(op + cl, key=lambda x: x["start"])
        # моно: единый поток. Роли назначит отдельная диаризация (pyannote) — заглушка speaker=unknown.
        mono = audio.mean(axis=1)
        segs = self._transcribe_stream(mono, sr, "unknown")
        return segs


class YandexSTT:
    """
    Yandex SpeechKit (кандидат №1 по §5, заточен под колл-центры СНГ).
    Реализуется при наличии бюджета/гранта. Контракт тот же transcribe(...).
    Здесь — каркас под API v3 (recognizeFileAsync); заполнить api_key и folder_id.
    """
    def __init__(self, api_key: str, folder_id: str):
        self.api_key = api_key
        self.folder_id = folder_id

    def transcribe(self, audio_path: str, channel: str = "mono") -> list[dict[str, Any]]:
        raise NotImplementedError(
            "Включается на бейк-офе с бюджетом на облачный STT. "
            "Контракт совместим: вернёт list[{speaker,start,end,text,lang}]."
        )


def get_engine(mode: str, **kwargs) -> STTEngine:
    mode = (mode or "mock").lower()
    if mode == "whisper":
        return WhisperSTT(**{k: v for k, v in kwargs.items() if k in {"model_size", "device", "compute_type"}})
    if mode == "yandex":
        return YandexSTT(kwargs["api_key"], kwargs["folder_id"])
    return MockSTT(kwargs.get("segments"))
