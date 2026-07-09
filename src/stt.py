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
        # Стерео: пробуем развести по каналам (оператор/клиент) — точная диаризация.
        if channel == "stereo":
            try:
                import soundfile as sf
                audio, sr = sf.read(audio_path, always_2d=True)
                if audio.shape[1] >= 2:
                    op = self._transcribe_stream(audio[:, 0], sr, "operator")
                    cl = self._transcribe_stream(audio[:, 1], sr, "client")
                    return sorted(op + cl, key=lambda x: x["start"])
            except Exception:
                pass  # не вышло прочитать как стерео — уходим в надёжный моно-путь ниже
        # Надёжный путь: faster-whisper сам декодирует файл (в т.ч. mp3).
        # Роли не размечены (speaker=unknown) — их определит LLM по смыслу.
        out = []
        segments, _info = self.model.transcribe(audio_path, language=None, vad_filter=True)
        for s in segments:
            out.append({"speaker": "unknown", "start": round(s.start, 2), "end": round(s.end, 2),
                        "text": s.text.strip(), "lang": "auto"})
        return out


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


class DeepgramSTT:
    """
    Облачная транскрипция через Deepgram (без GPU и локальной установки).
    Модель по умолчанию — nova-3 с language=multi (смесь RU/KZ). Для лучшего качества
    на казахском можно переключить на Whisper Cloud: model="whisper-large".

    Диаризация из коробки:
      - stereo -> multichannel=True: канал 0 = оператор, канал 1 = клиент (надёжнее всего);
      - mono   -> diarize=True: Deepgram сам разводит говорящих (speaker 0/1),
        роли (кто оператор) финально уточнит LLM по смыслу.
    Выход тот же: list[{speaker, start, end, text, lang}].
    """
    ENDPOINT = "https://api.deepgram.com/v1/listen"

    def __init__(self, api_key: str, model: str = "nova-3", language: str = "multi"):
        self.api_key = api_key
        self.model = model
        self.language = language

    def _params(self, channel: str) -> dict[str, Any]:
        is_whisper = self.model.startswith("whisper")
        p = {"model": self.model, "punctuate": "true", "utterances": "true"}
        if is_whisper:
            # Whisper Cloud: не понимает language=multi и smart_format.
            # У него свой авто-детект языка -> язык не передаём (кроме явного кода вроде "ru").
            if self.language and self.language not in ("multi", "auto"):
                p["language"] = self.language
        else:
            # nova и прочие: мультиязык + умное форматирование
            p["language"] = self.language
            p["smart_format"] = "true"
        if channel == "stereo":
            p["multichannel"] = "true"   # разводим по каналам записи
        else:
            p["diarize"] = "true"         # разводим говорящих внутри одного канала
        return p

    def transcribe(self, audio_path: str, channel: str = "mono") -> list[dict[str, Any]]:
        import mimetypes, requests
        mime = mimetypes.guess_type(audio_path)[0] or "audio/mpeg"
        with open(audio_path, "rb") as f:
            audio = f.read()
        resp = requests.post(
            self.ENDPOINT,
            params=self._params(channel),
            headers={"Authorization": f"Token {self.api_key}", "Content-Type": mime},
            data=audio, timeout=300,
        )
        resp.raise_for_status()
        return self._parse(resp.json(), channel)

    def _parse(self, data: dict[str, Any], channel: str) -> list[dict[str, Any]]:
        detected = (data.get("metadata", {}) or {}).get("detected_language") or "auto"
        # utterances — самый удобный формат: готовые реплики с началом/концом и говорящим
        utts = (data.get("results", {}) or {}).get("utterances") or []
        out = []
        for u in utts:
            text = (u.get("transcript") or "").strip()
            if not text:
                continue
            # stereo: channel 0->operator, 1->client. mono: speaker 0->operator, 1->client (эвристика, LLM уточнит)
            idx = u.get("channel", 0) if channel == "stereo" else u.get("speaker", 0)
            speaker = "operator" if idx == 0 else "client"
            out.append({"speaker": speaker, "start": round(float(u.get("start", 0)), 2),
                        "end": round(float(u.get("end", 0)), 2), "text": text,
                        "lang": u.get("language") or detected})
        if out:
            return sorted(out, key=lambda x: x["start"])
        # запасной путь, если utterances пуст — берём слова из первого канала
        chans = (data.get("results", {}) or {}).get("channels") or []
        if chans:
            alt = (chans[0].get("alternatives") or [{}])[0]
            txt = (alt.get("transcript") or "").strip()
            if txt:
                return [{"speaker": "unknown", "start": 0.0, "end": 0.0, "text": txt, "lang": detected}]
        return out


def _looks_kazakh(segments: list[dict[str, Any]]) -> bool:
    """Эвристика: русский Deepgram выдаёт кириллицу; на казахском он выдаёт латинскую
    кашу / не-русский язык / слишком мало текста. Тогда — эскалация на ElevenLabs."""
    text = " ".join(s.get("text", "") for s in segments).strip()
    if not text:
        return True
    cyr = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
    lat = sum(1 for c in text if "a" <= c.lower() <= "z")
    letters = cyr + lat
    if letters and cyr / letters < 0.5:
        return True
    langs = {(s.get("lang") or "").lower() for s in segments}
    ru_like = {"ru", "russian", "en", "english", "multi", "auto", ""}
    if langs and not (langs & ru_like):
        return True
    return False


class RoutingSTT:
    """Дешёвый Deepgram сначала; если звонок похож на казахский — ElevenLabs Scribe."""

    def __init__(self, deepgram: "DeepgramSTT", kazakh):
        self.deepgram = deepgram
        self.kazakh = kazakh

    def transcribe(self, audio_path: str, channel: str = "mono") -> list[dict[str, Any]]:
        segs = self.deepgram.transcribe(audio_path, channel)
        if _looks_kazakh(segs):
            return self.kazakh.transcribe(audio_path, channel)
        return segs


def get_engine(mode: str, **kwargs) -> STTEngine:
    mode = (mode or "mock").lower()
    if mode == "whisper":
        return WhisperSTT(**{k: v for k, v in kwargs.items() if k in {"model_size", "device", "compute_type"}})
    if mode == "deepgram":
        return DeepgramSTT(kwargs["api_key"],
                           model=kwargs.get("model", "nova-3"),
                           language=kwargs.get("language", "multi"))
    if mode == "elevenlabs":
        from src.elevenlabs_stt import ElevenLabsSTT
        return ElevenLabsSTT(kwargs["api_key"], model=kwargs.get("model", "scribe_v2"))
    if mode == "route":
        from src.elevenlabs_stt import ElevenLabsSTT
        dg = DeepgramSTT(kwargs["deepgram_key"],
                         model=kwargs.get("deepgram_model", "nova-3"),
                         language=kwargs.get("deepgram_language", "multi"))
        el = ElevenLabsSTT(kwargs["elevenlabs_key"], model=kwargs.get("elevenlabs_model", "scribe_v2"))
        return RoutingSTT(dg, el)
    if mode == "yandex":
        return YandexSTT(kwargs["api_key"], kwargs["folder_id"])
    return MockSTT(kwargs.get("segments"))