# TJR Copy-Trade Bot — SMC/ICT Scanner + AI Executor

Production-grade, zero-cost automated trading bot implementing TJR's Smart
Money Concepts: 5-minute liquidity sweeps, Market Structure Shifts (MSS), and
Fair Value Gaps (FVG) during the NY open (9:30–11:00 AM ET).

## Architecture

```
┌─────────────────────────┐      POST /execute       ┌──────────────────────────┐
│  GitHub Actions (cron)  │ ───────────────────────► │  Render Flask Executor   │
│  scanner.py every 5 min │   passphrase header +    │  server.py (24/7)        │
│  screener → bars → SMC  │   trade card JSON        │  ├─ 10% cap re-check     │
│  math → 10% sizing      │                          │  ├─ Gemini 2.5 Flash     │
└─────────────────────────┘                          │  │  + Robinhood MCP      │
                                                     │  └─ Discord alert        │
                       UptimeRobot ──GET /health──►  └──────────────────────────┘
```

**Design rule (from the vault):** the LLM never touches numbers. The scanner
computes every value deterministically; the server re-verifies the 10% cap in
code *before* the model sees the card; Gemini only orchestrates the MCP tool
call and reports back.

## Setup

1. **Alpaca** — free account at alpaca.markets → paper trading keys →
   `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`.
2. **Gemini** — free key at aistudio.google.com → `GEMINI_API_KEY`
   (Vertex express keys starting with `AQ.` are auto-detected).
3. **Discord** — server settings → Integrations → Webhooks → copy URL →
   `DISCORD_WEBHOOK_URL`. For push pings, enable Developer Mode and copy your
   **numeric** user ID → `DISCORD_USER_ID`.
4. **Render** — new Web Service from this repo:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn server:app`
   - Env vars: `GEMINI_API_KEY`, `DISCORD_WEBHOOK_URL`, `DISCORD_USER_ID`,
     `WEBHOOK_PASSPHRASE`, `ROBINHOOD_MCP_URL`, `ROBINHOOD_MCP_HEADERS`,
     Alpaca keys (for the server-side 10% pre-check).
5. **UptimeRobot** — free HTTP monitor pinging `https://<your-render-app>/health`
   every 5 min during market hours (keeps the free tier awake).
6. **GitHub Secrets** — `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
   `RENDER_WEBHOOK_URL`, `WEBHOOK_PASSPHRASE`.

## Local test

```bash
cp .env.example .env   # fill in values
pip install -r requirements.txt

python test_strategy.py      # 9 deterministic SMC checklist tests (no deps beyond pandas)
python -c "from screener import get_daily_dynamic_watchlist; print(get_daily_dynamic_watchlist())"
python scanner.py            # full scan (fires signals only inside 9:30–11:00 ET)
python server.py             # executor on http://localhost:10000
curl http://localhost:10000/health
```

## Strategy checklist (all must pass)

| # | Check | Rule |
|---|-------|------|
| 1 | Session | 09:30–11:00 ET, Mon–Fri, closed bars only |
| 2 | Liquidity Sweep | wick beyond prior 20-bar high/low, close back inside |
| 3 | MSS | close displaces beyond prior 10-bar swing extreme |
| 4 | FVG | 3-candle imbalance ≥ 0.15% of price, same direction |
| 5 | Sizing | `quantity = (cash × 0.10) / close`, 4dp fractional |

## Known deviations from the original spec

- `yfinance` / `smartmoneyconcepts` are listed per spec but currently unused
  (the SMC math is implemented deterministically in `strategy.py`). Safe to
  prune if you want a leaner install.
- The workflow uses the `timezone:` schedule field (GA March 2026). If your
  runner predates it, switch the cron to UTC: `*/5 13-14 * * 1-5` + `0 15`.
- Gemini's MCP tool-loop requires the async client (`client.aio`) — handled.

## ⚠️ Risk notes (read the vault's 03 Risk Register)

- Robinhood MCP automation sits in a ToS gray zone — keep size small; the
  account (not you) bears restriction risk. Verify stop-loss attachment
  support before scaling beyond Phase 2.
- Paper-trade first. `ALPACA_PAPER=paper` (default) reads paper balance for
  the 10% cap; flip deliberately.
- Nothing here is financial advice.
