# g2-alerts
Phone alerts for public Polymarket wallets (read-only), with a suggested copy size.

Watches gopfan2 and mr.ozi every ~5 minutes and sends an ntfy alert when either trades.

How the copy size works (defaults, change via env vars in the workflow):
- Bankroll $100, split $50 per trader.
- They mostly use limit orders that fill bit by bit, so one batch of fills says little about the bet. Each time he buys, the script looks up his whole position in that outcome and sets your target = his position cost x $50 / his deployed capital (gopfan2 about $340k, mr.ozi about $400k). A $20k position means a $2.50 target.
- The alert tells you to buy the gap between that target and what you were already told to buy, so as his order keeps filling you get top-up alerts. The target is capped at $10 per market (10% of bankroll), and gaps under $1 are not alerted.
- Sells are alerted only for markets you were told to buy, as "SELL N% of your shares" (mr.ozi often sells before resolution, so act on these).

Set `SHOW_ALL=1` to get every trade again, and refresh the `capital` figures in `notify.py` now and then.
