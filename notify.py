#!/usr/bin/env python3
"""
notify.py - push a phone alert (via ntfy) when a copied Polymarket wallet trades, with a suggested copy size.
Read-only: public data-api, no keys, no trading.

Meant to run every ~5 minutes on GitHub Actions. State (which fills it has already told you about) lives in state.json.
Fills are grouped per trader+market+side, so 460 tiny fills in one market become ONE alert.

Copy sizing: your BANKROLL is split equally between the traders. They mostly use limit orders that fill bit by bit,
so a single batch of fills says little about the size of the bet. Instead, each time he buys, the alert looks up his
WHOLE position in that outcome and sets your target = his position cost x (your share of the bankroll / his typical
deployed capital), capped at MAX_COPY_PCT of the bankroll. You are told to buy the gap between that target and what
you were already told to buy; a gap under MIN_ORDER_USD is not alerted (it may become one as more of his order fills). Sells are alerted only for markets you were told
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


def his_position(wallet, condition_id, asset):
    """(shares, cost in $) he holds in one outcome token now, or None if the lookup fails."""
    j = get_list("/positions", {"user": wallet, "market": condition_id, "sizeThreshold": 0})
    if j is None:
        return None
    ps = [p for p in j if str(p.get("asset")) == str(asset)]
    shares = sum(float(p.get("size") or 0) for p in ps)
    cost = sum(float(p.get("initialValue") or float(p.get("size") or 0) * float(p.get("avgPrice") or 0)) for p in ps)
    return shares, cost


def key(r):
    return f"{r.get('transactionHash')}|{r.get('asset')}|{r.get('side')}|{r.get('size')}|{r.get('price')}"


def push(title, message, click=None, priority=3, tags=None):
    body = {"topic": TOPIC, "title": title, "message": message, "priority": priority, "tags": tags or []}
    if click:
        body["click"] = click
    http(NTFY + "/", json.dumps(body).encode())


def money(x):
    return f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"


def copy_line(t, i, copied):
    """Suggested action for your account, and the $ to buy (BUY) or % to sell (SELL).
    Buys are sized off his whole position in the market, not this one batch of fills: his limit orders fill
    bit by bit, so each alert tops your position up to its target and says how much you should hold in total."""
    scale = (BANKROLL / len(TRADERS)) / t["capital"]
    pos = his_position(t["wallet"], i["cid"], i["asset"])
    have = copied.get(i["asset"], {}).get("usd", 0.0)
    if i["side"] == "BUY":
        his_cost = pos[1] if pos else i["usd"]
        target = min(his_cost * scale, MAX_COPY)
        amt = target - have
        if amt < MIN_ORDER:
            return f"Copy: nothing yet (target {money(target)}, you have {money(have)})", None
        return (f"Copy: BUY {money(amt)} of {i['outcome']} up to {min(i['px'] + 0.02, 0.99):.2f}\n"
                f"Target {money(target)} in this market; he holds {money(his_cost)}"), amt
    if pos is None:
        return "Copy: sell your position in proportion (could not read his remaining size)", None
    held = pos[0]
    if held < 1:
        return "Copy: he has exited, SELL all of yours", 100
    pct = 100 * i["shares"] / (i["shares"] + held)
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
    copied = ts.get("copied", {})  # asset -> {"title", "usd" you were told to buy so far}
    items = []
    for k in list(pending):
        p = pending[k]
        if p["shares"] <= 0:
            del pending[k]
        elif p["usd"] >= MIN_USD or (nowt - p["first"] > FLUSH_S and p["usd"] >= FLUSH_MIN_USD):
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
        i["line"], amt = copy_line(t, i, copied)
        if i["side"] == "BUY" and amt is None and not SHOW_ALL:
            continue
        if i["side"] == "BUY" and amt is not None:
            c = copied.setdefault(i["asset"], {"title": i["title"], "usd": 0.0})
            c["usd"] += amt
        if i["side"] == "SELL" and amt == 100:
            copied.pop(i["asset"], None)
        elif i["side"] == "SELL" and amt is not None and i["asset"] in copied:
            copied[i["asset"]]["usd"] *= 1 - amt / 100
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
