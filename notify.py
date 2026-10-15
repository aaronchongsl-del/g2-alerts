#!/usr/bin/env python3
"""
notify.py - push a phone alert (via ntfy) when a copied Polymarket wallet trades, with a suggested copy size.
Read-only: public data-api, no keys, no trading.

Meant to run every ~5 minutes on GitHub Actions. State (which fills it has already told you about) lives in state.json.
Fills are grouped per trader+market+side, so 460 tiny fills in one market become ONE alert.

Copy sizing: your BANKROLL is split equally between the traders. Each trader's buy is scaled by
(your share of the bankroll / that trader's typical deployed capital), capped at MAX_COPY_PCT of the bankroll.
Buys whose suggested size is under MIN_ORDER_USD are not alerted. Sells are alerted only for markets you were told
to buy (remembered in state.json), and say what percent of his position he sold, so you sell the same percent of
yours (mr.ozi often exits before resolution).

Env vars:
  NTFY_TOPIC    required  your private ntfy topic (store as a GitHub secret)
  TRADERS       optional  JSON list of {"name", "wallet", "capital"} to override the defaults below
  BANKROLL      default 100   your copy account in $
  MAX_COPY_PCT  default 10    max suggested buy per alert, % of bankroll
  MIN_ORDER_USD default 1     smallest order worth placing
  MIN_USD       default 25    alert once a market+side's running total reaches this (small fills accumulate across runs)
  FLUSH_MINUTES default 60    a running total older than this is sent if >= FLUSH_MIN_USD, otherwise dropped
  FLUSH_MIN_USD default 10
  BIG_USD       default 1000  trades at/above this get high priority
  MAX_ALERTS    default 6     separate alerts per trader per run; the rest are rolled into one summary
  NTFY_SERVER   default https://ntfy.sh
  SHOW_ALL      set to 1 to also alert buys too small to copy and sells of markets you were not told to buy
  TEST          set to 1 to send a test notification and exit
"""
import json, os, sys, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

DATA = os.environ.get("DATA_API", "https://data-api.polymarket.com")
NTFY = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
# capital = average cost of his open positions over the last ~90 days (Oct 2026 estimate from his fills); refresh now and then
DEFAULT_TRADERS = [
    {"name": "gopfan2", "wallet": "0xf2f6af4f27ec2dcf4072095ab804016e14cd5817", "capital": 340000},
    {"name": "mr.ozi", "wallet": "0x614dc8d3542c12103d2c6a3553fd761e391d1546", "capital": 400000},
]
TRADERS = json.loads(os.environ["TRADERS"]) if os.environ.get("TRADERS") else DEFAULT_TRADERS
BANKROLL = float(os.environ.get("BANKROLL", "100"))
MAX_COPY = BANKROLL * float(os.environ.get("MAX_COPY_PCT", "10")) / 100
MIN_ORDER = float(os.environ.get("MIN_ORDER_USD", "1"))
MIN_USD = float(os.environ.get("MIN_USD", "25"))
BIG_USD = float(os.environ.get("BIG_USD", "1000"))
MAX_ALERTS = int(os.environ.get("MAX_ALERTS", "6"))
STATE = Path(os.environ.get("STATE_FILE", "state.json"))
FLUSH_S = int(os.environ.get("FLUSH_MINUTES", "60")) * 60
FLUSH_MIN_USD = float(os.environ.get("FLUSH_MIN_USD", "10"))
SHOW_ALL = os.environ.get("SHOW_ALL") == "1"
KEEP = 3000  # fill keys remembered per trader


def http(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def get_list(path, params):
    q = urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            st, body = http(f"{DATA}{path}?{q}")
            j = json.loads(body)
            if isinstance(j, list):
                return j
        except urllib.error.HTTPError as e:
            if e.code == 400:
                return None
            time.sleep(2 * (attempt + 1))
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def fetch_activity(wallet):
    for limit in (500, 200):
        j = get_list("/activity", {"user": wallet, "type": "TRADE", "limit": limit, "sortBy": "TIMESTAMP", "sortDirection": "DESC"})
        if j is not None:
            return j
    return None


def shares_held(wallet, condition_id, asset):
    """His current share count in one outcome token, or None if the lookup fails."""
    j = get_list("/positions", {"user": wallet, "market": condition_id, "sizeThreshold": 0})
    if j is None:
        return None
    return sum(float(p.get("size") or 0) for p in j if str(p.get("asset")) == str(asset))


def key(r):
    return f"{r.get('transactionHash')}|{r.get('asset')}|{r.get('side')}|{r.get('size')}|{r.get('price')}"


def push(title, message, click=None, priority=3, tags=None):
    body = {"topic": TOPIC, "title": title, "message": message, "priority": priority, "tags": tags or []}
    if click:
        body["click"] = click
    http(NTFY + "/", json.dumps(body).encode())


def money(x):
    return f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"


def copy_line(t, i):
    """Suggested action for your account."""
    if i["side"] == "BUY":
        amt = i["usd"] * (BANKROLL / len(TRADERS)) / t["capital"]
        if amt < MIN_ORDER:
            return f"Copy: too small (would be {money(amt)})", None
        amt = min(amt, MAX_COPY)
        return f"Copy: BUY {money(amt)} of {i['outcome']} up to {min(i['px'] + 0.02, 0.99):.2f}", amt
    held = shares_held(t["wallet"], i["cid"], i["asset"])
    if held is None:
        return "Copy: sell your position in proportion (could not read his remaining size)", None
    pct = 100 * i["shares"] / (i["shares"] + held) if i["shares"] + held > 0 else 100
    if held < 1:
        return "Copy: he has exited, SELL all of yours", 100
    return f"Copy: SELL {pct:.0f}% of your {i['outcome']} shares", pct


def run_trader(t, ts, nowt):
    name, wallet = t["name"], t["wallet"].lower()
    rows = fetch_activity(wallet)
    if rows is None:
        print(f"{name}: could not fetch activity")
        return False
    seen = list(ts.get("seen", []))
    seen_set = set(seen)
    first_run = "seen" not in ts
    new = [r for r in rows if key(r) not in seen_set]
    for r in sorted(new, key=lambda r: int(r["timestamp"])):
        seen.append(key(r))
        seen_set.add(key(r))
    if first_run:
        print(f"{name}: first run, remembered {len(new)} existing fills, no alerts sent")
        new = []
        push(f"{name} alerts are on", f"Watching {wallet[:8]}... You will get an alert with a copy size when he trades "
             f"(bankroll {money(BANKROLL)}, {money(BANKROLL / len(TRADERS))} for him).", tags=["eyes"])
    pending = ts.get("pending", {})
    for r in new:
        k = f"{r.get('conditionId')}|{r.get('asset')}|{r.get('side')}"
        p = pending.setdefault(k, dict(usd=0.0, shares=0.0, n=0, side=r.get("side"), title=r.get("title", "?"),
                                       outcome=r.get("outcome", "?"), first=int(r["timestamp"]),
                                       slug=r.get("eventSlug") or r.get("slug")))
        p["usd"] += float(r["usdcSize"]); p["shares"] += float(r["size"]); p["n"] += 1
        p["first"] = min(p["first"], int(r["timestamp"]))
    copied = ts.get("copied", {})  # asset -> title of markets you were told to buy
    buy_thr = max(MIN_USD, MIN_ORDER * t["capital"] * len(TRADERS) / BANKROLL)  # his buy size that makes a copyable order
    items = []
    for k in list(pending):
        p = pending[k]
        if p["shares"] <= 0:
            del pending[k]
        elif p["usd"] >= (buy_thr if p["side"] == "BUY" and not SHOW_ALL else MIN_USD) or (nowt - p["first"] > FLUSH_S and p["usd"] >= FLUSH_MIN_USD):
            cid, asset, _ = k.split("|")
            items.append(dict(side=p["side"], usd=p["usd"], shares=p["shares"], px=p["usd"] / p["shares"], n=p["n"],
                              title=p["title"], outcome=p["outcome"], cid=cid, asset=asset,
                              link=f"https://polymarket.com/event/{p['slug']}" if p.get("slug") else None))
            del pending[k]
        elif nowt - p["first"] > FLUSH_S:
            del pending[k]  # too small to bother you with, and too old to keep waiting on
    shown = []
    for i in sorted(items, key=lambda i: -i["usd"]):
        if i["side"] == "SELL" and i["asset"] not in copied and not SHOW_ALL:
            continue
        i["line"], amt = copy_line(t, i)
        if i["side"] == "BUY" and amt is None and not SHOW_ALL:
            continue
        if i["side"] == "BUY" and amt is not None:
            copied[i["asset"]] = i["title"]
        if i["side"] == "SELL" and amt == 100:
            copied.pop(i["asset"], None)
        shown.append(i)
    items = shown
    for i in items[:MAX_ALERTS]:
        line = i["line"]
        title = f"{name} {i['side']} {money(i['usd'])} {i['outcome']} @ {i['px']:.2f}"
        msg = f"{line}\n{i['title']}\n{i['shares']:,.0f} shares in {i['n']} fill{'s' if i['n'] > 1 else ''}"
        push(title, msg, i["link"], 4 if i["usd"] >= BIG_USD else 3, ["chart_with_upwards_trend" if i["side"] == "BUY" else "moneybag"])
    rest = items[MAX_ALERTS:]
    if rest:
        lines = [f"{i['side']} {money(i['usd'])} {i['outcome']} @ {i['px']:.2f}: {i['title'][:50]}" for i in rest[:15]]
        push(f"{name}: {len(rest)} more trades", "\n".join(lines), priority=2)
    print(f"{name}: {len(pending)} waiting below threshold; {len(new)} new fills -> {len(items)} worth copying -> "
          f"{min(len(items), MAX_ALERTS) + (1 if rest else 0)} alerts")
    ts.update(seen=seen[-KEEP:], pending=pending, copied=copied)
    return True


def main():
    if not TOPIC:
        sys.exit("NTFY_TOPIC is not set")
    if os.environ.get("TEST") == "1":
        push("copy alerts: test", "If you can read this on your phone, notifications work.", tags=["white_check_mark"])
        print("test notification sent")
        return
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if "traders" not in state:  # old single-wallet state belonged to gopfan2
        old = {k: state[k] for k in ("seen", "pending") if k in state}
        state = {"traders": {DEFAULT_TRADERS[0]["wallet"]: old} if old else {}}
    nowt, ok = int(time.time()), 0
    for t in TRADERS:
        ok += run_trader(t, state["traders"].setdefault(t["wallet"].lower(), {}), nowt)
    state["updated"] = nowt
    STATE.write_text(json.dumps(state))
    if not ok:
        sys.exit("could not fetch activity for any trader (API unreachable or blocked from this runner)")


if __name__ == "__main__":
    main()
