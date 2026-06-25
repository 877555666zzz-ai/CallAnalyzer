"""
Детерминированные метрики звонка (раздел 6.5).
Считаются из таймкодов диаризованного транскрипта — БЕЗ участия LLM,
потому что модели ненадёжны в арифметике (особенно с числами/паузами).
"""
from __future__ import annotations
from typing import Any


def compute_metrics(segments: list[dict[str, Any]], cfg_metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg_metrics = cfg_metrics or {}
    hold_threshold = float(cfg_metrics.get("pause_without_hold_threshold_sec", 3.0))
    short_call_sec = float(cfg_metrics.get("short_call_suspicious_sec", 45))

    if not segments:
        return {
            "talk_ratio_operator_pct": 0.0, "talk_ratio_client_pct": 0.0,
            "total_duration_sec": 0.0, "longest_pause_sec": 0.0, "longest_pause_at": None,
            "operator_reaction_avg_sec": None, "operator_reaction_max_sec": None,
            "pauses_without_hold": [], "short_call_flag": True,
        }

    segs = sorted(segments, key=lambda s: s["start"])

    # Время речи по ролям -> talk ratio
    speak = {"operator": 0.0, "client": 0.0}
    for s in segs:
        speak[s["speaker"]] = speak.get(s["speaker"], 0.0) + max(0.0, s["end"] - s["start"])
    total_speak = speak["operator"] + speak["client"]
    op_pct = round(100 * speak["operator"] / total_speak, 1) if total_speak else 0.0
    cl_pct = round(100 * speak["client"] / total_speak, 1) if total_speak else 0.0

    total_duration = round(segs[-1]["end"] - segs[0]["start"], 1)

    # Паузы между соседними репликами
    longest_pause = 0.0
    longest_pause_at = None
    reaction_times: list[float] = []   # клиент задал -> оператор ответил
    pauses_without_hold: list[dict[str, Any]] = []
    hold_markers = ("оставайтесь на линии", "минуту", "секунду", "подождите", "сейчас уточню", "не кладите трубку")

    for prev, nxt in zip(segs, segs[1:]):
        gap = round(nxt["start"] - prev["end"], 2)
        if gap <= 0:
            continue
        if gap > longest_pause:
            longest_pause, longest_pause_at = gap, prev["end"]
        # время реакции оператора на реплику клиента
        if prev["speaker"] == "client" and nxt["speaker"] == "operator":
            reaction_times.append(gap)
        # пауза без удержания: длинный разрыв, оператор не сказал «оставайтесь на линии»
        if gap >= hold_threshold:
            said_hold = any(m in prev["text"].lower() for m in hold_markers)
            if not said_hold:
                pauses_without_hold.append({
                    "start": prev["end"], "end": nxt["start"],
                    "duration": gap, "after_speaker": prev["speaker"],
                })

    return {
        "talk_ratio_operator_pct": op_pct,
        "talk_ratio_client_pct": cl_pct,
        "total_duration_sec": total_duration,
        "longest_pause_sec": longest_pause,
        "longest_pause_at": longest_pause_at,
        "operator_reaction_avg_sec": round(sum(reaction_times) / len(reaction_times), 2) if reaction_times else None,
        "operator_reaction_max_sec": round(max(reaction_times), 2) if reaction_times else None,
        "pauses_without_hold": pauses_without_hold,
        "short_call_flag": total_duration < short_call_sec,
    }
