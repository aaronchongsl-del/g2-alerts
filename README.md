# g2-alerts
Phone alerts for public Polymarket wallets (read-only), with a suggested copy size.

Watches gopfan2 and mr.ozi every ~5 minutes and sends an ntfy alert when either trades.

How the copy size works (defaults, change via env vars in the workflow):
- Bankroll $100, split $50 per trader.
- A buy is scaled by $50 / his deployed capital (gopfan2 about $340k, mr.ozi about $400k), so his $20k buy becomes about $3.
- Suggested buys are capped at $10 (10% of bankroll). Buys that would be under $1 are not alerted; that means roughly gopfan2 buys of $13.6k+ and mr.ozi buys of $16k+.
- Sells are alerted only for markets you were told to buy, as "SELL N% of your shares" (mr.ozi often sells before resolution, so act on these).

Set `SHOW_ALL=1` to get every trade again, and refresh the `capital` figures in `notify.py` now and then.
