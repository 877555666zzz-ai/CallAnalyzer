"""
Telegram-доставка (§8.2): периодические сводки «где деньги» + алерты по ред-флагам.
Отправка через Bot API (без тяжёлых зависимостей). Текст сводки берём из report_money.render_telegram.

Запуск разово (например, по cron вечером):
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python -m src.telegram_report
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import Any

import requests

API = "https://api.telegram.org"


def send_message(text: str, token: str | None = None, chat_id: str | None = None) -> dict[str, Any]:
    token = token or os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = chat_id or os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"{API}/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
                            "disable_web_page_preview": True}, timeout=30)
    r.raise_for_status()
    return r.json()


def redflag_alerts(report: dict[str, Any], dashboard_base: str | None = None) -> list[str]:
    """Алерты: операторы с аномально большим числом high-ред-флагов."""
    msgs = []
    for name, cnt in report.get("redflag_alerts_operators", {}).items():
        if cnt >= 2:
            link = f"\n{dashboard_base}/calls?manager={name}&redflags=true" if dashboard_base else ""
            msgs.append(f"🚨 *Алерт*: у оператора *{name}* {cnt} звонк(а/ов) с серьёзными ред-флагами "
                        f"(запрещённые фразы/угрозы). Проверьте.{link}")
    return msgs


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.db import get_engine, get_sessionmaker
    from src.report_money import build_money_report, render_telegram
    from src.analyzer import load_config

    cfg = load_config(Path(__file__).resolve().parent.parent / "configs" / "yandex_taxi_corp.yaml")
    Session = get_sessionmaker(get_engine(os.environ.get("DATABASE_URL")))
    dash = os.environ.get("DASHBOARD_BASE")
    with Session() as s:
        report = build_money_report(s, cfg["economics"])
    summary = render_telegram(report)
    if dash:
        summary += f"\n\n[Открыть дашборд]({dash})"
    send_message(summary)
    for alert in redflag_alerts(report, dash):
        send_message(alert)
    print("Отправлено в Telegram.")


if __name__ == "__main__":
    main()
