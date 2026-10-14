#!/usr/bin/env python3
"""
notify.py - push a phone alert (via ntfy) when a Polymarket wallet trades. Read-only: public data-api, no keys, no trading.

Meant to run every ~5 minutes on GitHub Actions. State (which fills it has already told you about) lives in state.json.
Fills are grouped per market+side, so 460 tiny fills in one market become ONE alert.

Env vars:
  NTFY_TOPIC   required  your private ntfy topic (store as a GitHub secret)
  WALLET       default gopfan2's proxy wallet
  MIN_USD      default 25    alert once a market+side's running total reaches this (small fills accumulate across runs)
  FLUSH_MINUTES default 60   a running total older than this is sent if >= FLUSH_MIN_USD, otherwise dropped
  FLUSH_MIN_USD default 10
  BIG_USD      default 1000  trades at/above this get high priority
  MAX_ALERTS   default 6     separate alerts per run; the rest are rolled into one summary
  NTFY_SERVER  default https://ntfy.sh
  TEST         set to 1 to send a test notification and exit
"""
import json, os, sys, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

DATA = os.environ.get("DATA_API", "https://data-api.polymarket.com")
NTFY = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
WALLET = os.environ.get("WALLET", "0xf2f6af4f27ec2dcf4072095ab804016e14cd5817").lower()
MIN_USD = float(os.environ.get("MIN_USD", "25"))
BIG_USD = float(os.environ.get("BIG_USD", "1000"))
MAX_ALERTS = int(os.environ.get("MAX_ALERTS", "6"))
STATE = Path(os.environ.get("STATE_FILE", "state.json"))
FLUSH_S = int(os.environ.get("FLUSH_MINUTES", "60")) * 60
FLUSH_MIN_USD = float(os.environ.get("FLUSH_MIN_USD", "10"))
KEEP = 3000  # fill keys remembered


def http(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def fetch_activity():
    for limit in (500, 200):
        q = urllib.parse.urlencode({"user": WALLET, "type": "TRADE", "limit": limit, "sortBy": "TIMESTAMP", "sortDirection": "DESC"})
        for attempt in range(3):
            try:
                st, body = http(f"{DATA}/activity?{q}")
                j = json.loads(body)
                if isinstance(j, list):
                    return j
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    break  # limit too high, try smaller
                time.sleep(2 * (attempt + 1))
            except Exception:
                time.sleep(2 * (attempt + 1))
    return None


def key(r):
    return f"{r.get('transactionHash')}|{r.get('asset')}|{r.get('side')}|{r.get('size')}|{r.get('price')}"


def push(title, message, click=None, priority=3, tags=None):
    body = {"topic": TOPIC, "title": title, "message": message, "priority": priority, "tags": tags or []}
    if click:
        body["click"] = click
    http(NTFY + "/", json.dumps(body).encode())


def money(x):
    return f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"


def main():
    if not TOPIC:
        sys.exit("NTFY_TOPIC is not set")
    if os.environ.get("TEST") == "1":
        push("gopfan2 alerts: test", "If you can read this on your phone, notifications work.", tags=["white_check_mark"])
        print("test notification sent")
        return
    rows = fetch_activity()
    if rows is None:
        sys.exit("could not fetch activity (API unreachable or blocked from this runner)")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    seen = list(state.get("seen", []))
    seen_set = set(seen)
    first_run = not state
    new = [r for r in rows if key(r) not in seen_set]
    for r in sorted(new, key=lambda r: int(r["timestamp"])):
        seen.append(key(r))
        seen_set.add(key(r))
    if first_run:
        print(f"first run: remembered {len(new)} existing fills, no alerts sent")
        new = []
        push("gopfan2 alerts are on", f"Watching {WALLET[:8]}... You will get an alert when he trades (min {money(MIN_USD)}).", tags=["eyes"])
    pending = state.get("pending", {})
    for r in new:
        k = f"{r.get('conditionId')}|{r.get('asset')}|{r.get('side')}"
        p = pending.setdefault(k, dict(usd=0.0, shares=0.0, n=0, side=r.get("side"), title=r.get("title", "?"),
                                       outcome=r.get("outcome", "?"), first=int(r["timestamp"]),
                                       slug=r.get("eventSlug") or r.get("slug")))
        p["usd"] += float(r["usdcSize"]); p["shares"] += float(r["size"]); p["n"] += 1
        p["first"] = min(p["first"], int(r["timestamp"]))
    items, nowt = [], int(time.time())
    for k in list(pending):
        p = pending[k]
        if p["shares"] <= 0:
            del pending[k]
        elif p["usd"] >= MIN_USD or (nowt - p["first"] > FLUSH_S and p["usd"] >= FLUSH_MIN_USD):
            items.append(dict(side=p["side"], usd=p["usd"], shares=p["shares"], px=p["usd"] / p["shares"], n=p["n"],
                              title=p["title"], outcome=p["outcome"],
                              link=f"https://polymarket.com/event/{p['slug']}" if p.get("slug") else None))
            del pending[k]
        elif nowt - p["first"] > FLUSH_S:
            del pending[k]  # too small to bother you with, and too old to keep waiting on
    items.sort(key=lambda i: -i["usd"])
    for i in items[:MAX_ALERTS]:
        t = f"gopfan2 {i['side']} {money(i['usd'])} {i['outcome']} @ {i['px']:.2f}"
        m = f"{i['title']}\n{i['shares']:,.0f} shares in {i['n']} fill{'s' if i['n'] > 1 else ''}"
        push(t, m, i["link"], 4 if i["usd"] >= BIG_USD else 3, ["chart_with_upwards_trend" if i["side"] == "BUY" else "moneybag"])
    rest = items[MAX_ALERTS:]
    if rest:
        lines = [f"{i['side']} {money(i['usd'])} {i['outcome']} @ {i['px']:.2f}: {i['title'][:50]}" for i in rest[:15]]
        push(f"gopfan2: {len(rest)} more trades", "\n".join(lines), priority=2)
    print(f"{len(pending)} waiting below threshold;", end=" ")
    print(f"{len(new)} new fills -> {len(items)} grouped trades -> {min(len(items), MAX_ALERTS) + (1 if rest else 0)} alerts")
    STATE.write_text(json.dumps({"seen": seen[-KEEP:], "pending": pending, "updated": int(time.time())}))


if __name__ == "__main__":
    main()
