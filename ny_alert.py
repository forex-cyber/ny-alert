#!/usr/bin/env python3
import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------- تنظیمات ----------------
NY = ZoneInfo("America/New_York")
BROKER_TZ = ZoneInfo("Europe/Athens")   # فقط برای نمایش در --test
TEHRAN = ZoneInfo("Asia/Tehran")        # فقط برای نمایش در --test
UTC = timezone.utc

LEAD = timedelta(hours=1)               # چقدر قبل از رویداد پیام بدهد

# (name, ساعت به وقت نیویورک, متن پیام)
EVENTS = [
    ("open", time(9, 30), "The New York market opens in 1 hour."),
    ("close", time(15, 55), "The market closes in 1 hour."),
]

# روزهایی که پیام نخواهی (تعطیلی‌ها). مثال: {"2026-11-26", "2026-12-25"}
SKIP_DATES: set[str] = set()

STATE_FILE = Path(__file__).with_name("ny_state.json")


# ---------------- منطق زمان ----------------
def event_utc(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=NY).astimezone(UTC)


def due_alerts(now: datetime) -> list[tuple[str, str]]:
    today = now.astimezone(NY).date()
    if today.weekday() >= 5 or today.isoformat() in SKIP_DATES:
        return []
    out = []
    for name, t, text in EVENTS:
        ev = event_utc(today, t)
        alert = ev - LEAD
        if alert <= now < ev:
            out.append((f"{today.isoformat()}-{name}", text))
    return out


# ---------------- حافظه (ضد پیام تکراری) ----------------
def load_state() -> set[str]:
    try:
        data = json.loads(STATE_FILE.read_text())
        return set(data) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def save_state(sent: set[str], now: datetime) -> None:
    cutoff = (now.astimezone(NY).date() - timedelta(days=3)).isoformat()
    keep = sorted(k for k in sent if k[:10] >= cutoff)
    STATE_FILE.write_text(json.dumps(keep))


# ---------------- تلگرام ----------------
def send_telegram(text: str) -> None:
    token = os.environ.get("NY_BOT_TOKEN")
    chat_id = os.environ.get("NY_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("NY_BOT_TOKEN and/or NY_CHAT_ID is not set")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text},
        timeout=20,
    )
    r.raise_for_status()


# ---------------- حالت‌های اجرا ----------------
def run_once() -> None:
    now = datetime.now(UTC)
    sent = load_state()
    failed = False
    due = due_alerts(now)
    if not due:
        print(f"{now:%Y-%m-%d %H:%M} UTC: nothing to send")
    for key, text in due:
        if key in sent:
            print("already sent:", key)
            continue
        try:
            send_telegram(text)
        except Exception as exc:
            print(f"failed to send {key}: {exc}", file=sys.stderr)
            failed = True
            continue
        sent.add(key)
        print("sent:", key)
    save_state(sent, now)
    if failed:
        sys.exit(1)


def test_mode() -> None:
    def fmt(dt: datetime, tz: ZoneInfo) -> str:
        return dt.astimezone(tz).strftime("%H:%M")

    today = datetime.now(NY).date()
    print("Next 7 days (weekdays only):\n")
    for i in range(7):
        d = today + timedelta(days=i)
        if d.weekday() >= 5 or d.isoformat() in SKIP_DATES:
            continue
        for name, t, _ in EVENTS:
            ev = event_utc(d, t)
            al = ev - LEAD
            print(
                f"{d:%a %d %b} {name:<5} | event {fmt(ev, NY)} New York = {fmt(ev, BROKER_TZ)} broker"
                f" | alert at {fmt(al, BROKER_TZ)} broker = {fmt(al, TEHRAN)} Tehran"
            )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="print upcoming schedule")
    ap.add_argument("--send-test", action="store_true", help="send one test message")
    ap.add_argument("--once", action="store_true", help="single check then exit")
    args = ap.parse_args()
    try:
        if args.test:
            test_mode()
        elif args.send_test:
            send_telegram("Test message: the New York market bot is working.")
            print("test message sent")
        else:
            run_once()
    except Exception as exc:
        print("error:", exc, file=sys.stderr)
        sys.exit(1)
