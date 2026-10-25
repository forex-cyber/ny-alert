#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------- settings ----------------
THRESHOLD = 20_000          # market cap that triggers the alert (USD)
FIRST_SEEN_MAX = 60_000     # a token first seen already above THRESHOLD alerts only if below this
MIN_WATCH = 5_000           # tokens below this are not tracked
WATCH_MAX = 450             # max tokens in the watch list
WATCH_TTL_HOURS = 6         # stop tracking a token after this many hours
NEW_POOLS_PAGES = 5         # pages of newest pools to scan (about 20 per page)
NETWORK = "solana"
LOCAL_TZ = ZoneInfo("Asia/Tehran")

API = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json;version=20230302", "User-Agent": "Mozilla/5.0"}
PAUSE = 2.3                 # seconds between API calls (free limit: 30 calls/min)
STATE_FILE = Path(__file__).with_name("meme_state.json")


class ApiProblem(Exception):
    pass


class ConfigError(Exception):
    pass


# ---------------- GeckoTerminal ----------------
def api_get(path, params=None):
    for attempt in (1, 2):
        try:
            r = requests.get(API + path, params=params, headers=HEADERS, timeout=30)
        except requests.RequestException as exc:
            raise ApiProblem(str(exc))
        if r.status_code == 429:
            if attempt == 1:
                time.sleep(20)
                continue
            raise ApiProblem("rate limited (429)")
        if r.status_code >= 400:
            raise ApiProblem(f"HTTP {r.status_code}")
        time.sleep(PAUSE)
        try:
            return r.json()
        except ValueError:
            raise ApiProblem("bad JSON")
    raise ApiProblem("request failed")


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_pools(payload):
    """Return {token_address: info} from a GeckoTerminal pools response."""
    included = {}
    for item in payload.get("included") or []:
        if item.get("type") == "token":
            included[item.get("id")] = item.get("attributes") or {}
    out = {}
    for pool in payload.get("data") or []:
        try:
            attrs = pool["attributes"]
            base_id = pool["relationships"]["base_token"]["data"]["id"]
            token = included.get(base_id, {})
            mcap = to_float(attrs.get("market_cap_usd"))
            if not mcap:
                mcap = to_float(attrs.get("fdv_usd"))   # unverified tokens: use FDV
            if not mcap:
                continue
            addr = token.get("address") or base_id.split("_", 1)[-1]
            name = token.get("name") or (attrs.get("name") or "").split(" / ")[0] or "Unknown"
            out[addr] = {
                "pool": attrs["address"],
                "name": name,
                "symbol": token.get("symbol") or "",
                "image": token.get("image_url"),
                "mcap": mcap,
            }
        except (KeyError, TypeError, AttributeError):
            continue
    return out


def fetch_multi(pools, into):
    for i in range(0, len(pools), 30):
        chunk = pools[i:i + 30]
        payload = api_get(
            f"/networks/{NETWORK}/pools/multi/{','.join(chunk)}", {"include": "base_token"}
        )
        into.update(parse_pools(payload))


# ---------------- Telegram ----------------
def tg(method, data):
    token = os.environ.get("MEME_BOT_TOKEN")
    chat_id = os.environ.get("MEME_CHAT_ID")
    if not token or not chat_id:
        raise ConfigError("MEME_BOT_TOKEN and/or MEME_CHAT_ID is not set")
    payload = dict(data, chat_id=chat_id)
    return requests.post(
        f"https://api.telegram.org/bot{token}/{method}", data=payload, timeout=30
    )


def good_image(url):
    return isinstance(url, str) and url.startswith("http") and "missing" not in url.lower()


def fmt_usd(v):
    return f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v / 1e3:.1f}K"


def build_caption(addr, info):
    when = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    title = info["name"] + (f" (${info['symbol']})" if info["symbol"] else "")
    return (
        f"{title}\n"
        f"This memecoin has reached ${THRESHOLD / 1000:.0f}K market cap! 🚀\n"
        f"Market cap now: {fmt_usd(info['mcap'])}\n"
        f"📅 {when} (Tehran time)\n\n"
        f"CA: {addr}\n"
        f"https://dexscreener.com/solana/{info['pool']}"
    )


def send_alert(addr, info):
    caption = build_caption(addr, info)
    if good_image(info.get("image")):
        r = tg("sendPhoto", {"photo": info["image"], "caption": caption})
        if r.ok:
            return
        print(f"photo failed ({r.status_code}), sending text only", file=sys.stderr)
    r = tg("sendMessage", {"text": caption})
    r.raise_for_status()


# ---------------- state ----------------
def load_state():
    try:
        d = json.loads(STATE_FILE.read_text())
        return {"alerted": dict(d.get("alerted", {})), "watch": dict(d.get("watch", {}))}
    except (OSError, ValueError, AttributeError, TypeError):
        return {"alerted": {}, "watch": {}}


def save_state(alerted, watch):
    STATE_FILE.write_text(json.dumps({"alerted": alerted, "watch": watch}))


# ---------------- main logic ----------------
def run_once():
    now = time.time()
    state = load_state()
    alerted, watch = state["alerted"], state["watch"]

    obs = {}
    try:
        for page in range(1, NEW_POOLS_PAGES + 1):
            payload = api_get(
                f"/networks/{NETWORK}/new_pools", {"include": "base_token", "page": page}
            )
            obs.update(parse_pools(payload))
        stale = [
            w.get("pool")
            for a, w in watch.items()
            if a not in obs and a not in alerted and w.get("pool")
        ]
        if stale:
            fetch_multi(stale, obs)
    except ApiProblem as exc:
        print("warning:", exc)

    sent = 0
    for addr, info in list(obs.items()):
        if addr in alerted:
            watch.pop(addr, None)
            continue
        prev = watch.get(addr)
        mcap = info["mcap"]

        if mcap >= THRESHOLD:
            crossed = (prev is not None and prev.get("mcap", 0) < THRESHOLD) or (
                prev is None and mcap <= FIRST_SEEN_MAX
            )
            if crossed:
                try:
                    send_alert(addr, info)
                except ConfigError:
                    raise
                except Exception as exc:
                    print(f"send failed for {info['name']}: {exc}", file=sys.stderr)
                    continue            # keep state so it is retried next run
                alerted[addr] = now
                sent += 1
                print(f"alert: {info['name']} {fmt_usd(mcap)}")
            watch.pop(addr, None)
        elif mcap >= MIN_WATCH:
            first = prev.get("first_seen", now) if prev else now
            watch[addr] = dict(info, first_seen=first)
        else:
            watch.pop(addr, None)

    # cleanup
    cutoff = now - WATCH_TTL_HOURS * 3600
    watch = {a: w for a, w in watch.items() if w.get("first_seen", 0) >= cutoff}
    if len(watch) > WATCH_MAX:
        top = sorted(watch.items(), key=lambda kv: kv[1].get("mcap", 0), reverse=True)
        watch = dict(top[:WATCH_MAX])
    alerted = {a: t for a, t in alerted.items() if t >= now - 7 * 86400}

    save_state(alerted, watch)
    print(f"done: scanned {len(obs)} tokens, watching {len(watch)}, alerts sent {sent}")


def test_mode():
    payload = api_get(f"/networks/{NETWORK}/new_pools", {"include": "base_token", "page": 1})
    tokens = parse_pools(payload)
    print(f"{len(tokens)} tokens on page 1 (highest market cap first):\n")
    for addr, t in sorted(tokens.items(), key=lambda kv: kv[1]["mcap"], reverse=True)[:15]:
        print(f"{t['name'][:28]:<28} {t['symbol'][:10]:<10} {fmt_usd(t['mcap']):>10}  image={'yes' if good_image(t['image']) else 'no'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="print newest tokens, send nothing")
    ap.add_argument("--send-test", action="store_true", help="send one test message")
    ap.add_argument("--once", action="store_true", help="single check then exit")
    args = ap.parse_args()
    try:
        if args.test:
            test_mode()
        elif args.send_test:
            r = tg("sendMessage", {"text": "Test message: the memecoin 20K bot is working."})
            r.raise_for_status()
            print("test message sent")
        else:
            run_once()
    except Exception as exc:
        print("error:", exc, file=sys.stderr)
        sys.exit(1)
