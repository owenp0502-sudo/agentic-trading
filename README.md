# TJR Copy-Trade Bot — SMC/ICT Scanner + AI Executor

Production-grade, zero-cost automated trading bot implementing TJR's Smart
Money Concepts: liquidity sweeps, Market Structure Shifts (MSS), and Fair
Value Gaps (FVG) on 5-minute bars during the NY open (9:30–11:00 AM ET).

## Architecture

Everything runs **inside one GitHub Actions job** — no Render, no always-on
host, nothing to keep awake. $0/month.

```
┌──────────────────────────────────────────────────────────┐
│  GitHub Actions — scanner.py every 5 min, 9:30–11:00 ET  │
│  (+ 11:00 / 11:05 close-out crons)                       │
│  screener → 5m bars (resampled from 1m) → SMC math →     │
│  10% sizing → executor.py:                               │
│    1. deterministic pre-checks vs LIVE Robinhood account │
│       (10% cap · min notional · one-position rule)       │
│    2. Gemini Flash + Robinhood MCP place the order       │
│    3. exits.py attaches bracket (structural stop GTC +   │
│       2R target GFD), monitors every run, flattens at    │
│       11:00 ET — all deterministic, LLM excluded         │
│    4. Discord alert on every entry/exit/ratchet/refusal  │
└──────────────────────────────────────────────────────────┘
```

**Design rule (from the vault):** the LLM never touches numbers. The scanner
computes every value deterministically; `executor.py` re-verifies the cap in
code *before* the model sees the card; Gemini only orchestrates the MCP tool
call and reports back. `server.py` remains as an optional HTTP wrapper for
manual drills.

## Setup

1. **Alpaca** — free account at alpaca.markets → paper trading keys →
   `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`.
2. **Gemini** — free key at aistudio.google.com → `GEMINI_API_KEY`
   (Vertex express keys starting with `AQ.` are auto-detected).
3. **Discord** — server settings → Integrations → Webhooks → copy URL →
   `DISCORD_WEBHOOK_URL`. For push pings, enable Developer Mode and copy your
   **numeric** user ID → `DISCORD_USER_ID`.
4. **GitHub Secrets** (set via `bash .set_gh_secrets.sh` after `gh auth login`):
   `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `GEMINI_API_KEY`, `GEMINI_MODEL`,
   `DISCORD_WEBHOOK_URL`, `DISCORD_USER_ID`, `WEBHOOK_PASSPHRASE`,
   `ROBINHOOD_TOKEN_STORE_JSON` (the local token file's contents).
5. **Go live**: the workflow ships with `EXECUTOR_DRY_RUN: "1"` — real scans,
   simulated fills. Flip to `"0"` when the dry-run alerts look right.

## Local test

```bash
cp .env.example .env   # fill in values
pip install -r requirements.txt

python test_strategy.py      # 9 SMC checklist tests
python test_exits.py         # 32 exit-strategy tests (levels, ratchet, parsers)
python scanner.py --selftest # synthetic end-to-end drill + Discord ping
python scanner.py --flatten  # manual close-out (no LLM)
python backtest.py --days 20 --slippage-bps 2   # replay real sessions
python -c "from executor import mcp_preflight; print(mcp_preflight())"
python scanner.py            # full scan (fires signals only inside 9:30–11:00 ET)
```

## Strategy checklist (all must pass)

| # | Check | Rule |
|---|-------|------|
| 1 | Session | 09:30–11:00 ET, Mon–Fri, closed bars only |
| 2 | Liquidity Sweep | wick beyond prior 20-bar high/low, close back inside |
| 3 | MSS | close displaces beyond prior 10-bar swing extreme |
| 4 | FVG | 3-candle imbalance ≥ 0.15% of price, same direction |
| 5 | Sizing | `quantity = (cash × 0.10) / close`, 4dp fractional |

## Exit strategy (all deterministic — the LLM never sets exits)

| Component | Rule |
|---|---|
| Stop | structural: just beyond the sweep extreme (setup invalidation), buffered 5bps, clamped to 0.1–2% of cost; falls back to `STOP_LOSS_PCT` (0.5%) |
| Target | `TAKE_PROFIT_R` × actual stop distance (2R default), forever anchored to initial risk |
| Resting bracket | whole-share `stop_market` GTC + `limit` GFD placed at entry (fractional remainder exits via monitor/flatten) |
| Trail | 5-min monitor ratchets the resting stop: breakeven at +1R (`TRAIL_BREAKEVEN_R`), optional %-trail (`TRAIL_STOP_PCT`), monotonic, broker-side stop = source of truth |
| Close-out | flatten all + cancel resting at `FLATTEN_TIME` (11:00 ET; 11:05 safety cron; `--flatten` manual) |

All knobs are env-tunable — see `config.py`.

## Research log — parameter & timeframe studies

Methodology shared by all studies: data = Alpaca IEX 1m bars resampled to
true 5m/15m OHLCV; signals and exits from the *same* modules the live bot
runs (`strategy.py` / `exits.py`); production-parity simulation (46-bar
rolling context, next-bar-open entries, stop-before-target inside bars,
5-min ratchet parity, flatten at session end); sizing 10% of $25k, one
position at a time, long-only; Robinhood commissions $0.

### Sep 25, 2026 — 60-day study (44 sessions × 12 liquid symbols)

**Funnel (5m, SPY, 8 days):** of 369 in-context bars — 82 close beyond the
prior-10 swing extreme, 37 are sweeps, but sweep→MSS-within-5-bars chains
occurred **twice**; the FVG filter narrows further. The temporal coupling
(MSS must be the current bar) is the frequency bottleneck, not the FVG gate.

| Experiment | Sample | Trades | Win rate | Avg R | PF | P&L |
|---|---|---|---|---|---|---|
| Strict FVG, live window, 5m | 44d × 12 | 0 | — | — | — | $0 |
| Strict FVG, extended 9:30–15:55, 5m | 44d × 12 | 2 | 0% | −1.20R | 0.00 | −$6 |
| No-FVG variant, extended, 5m | 20d × 6 | 8 | 38% | −0.20R | 0.39 | −$26 |
| Strict FVG, live window, **15m** | 44d × 12 | **41** | 32% | −0.32R | 0.55 | −$100 |
| Strict FVG, extended, **15m** | 44d × 12 | 42 | 24% | −0.39R | 0.60 | −$198 |

**Findings**
1. The strict sequence is a once-a-quarter event on 5m large-caps
   (~0.004 signals/symbol-day). Live-window frequency is zero, extended is
   marginal — parameter relaxation does not fix it.
2. **15m bars fix frequency (0 → 41 trades) but not quality (PF still < 1,
   avg −0.32R).** The timeframe hypothesis is half-confirmed: the sequence
   exists at 15m, but as-tested it has no edge. Winners were dominated by
   time-flattens (16 of 35 closed trades), not 2R targets.
3. Dropping the FVG gate trades 8× more often and loses money (PF 0.39).
   The confirmation gate earns its keep — keep `FVG_REQUIRED=1`.
4. Microstructure drag: with the 0.1% stop-clamp floor, 2bps round-trip
   slippage costs ~0.2R per trade. Prefer `MIN_STOP_DISTANCE_PCT ≥ 0.002`
   for any tight-stop variant.

**Conclusions / next steps**
- Keep the bot strict and live-window; it is a patient hunter by design.
- Most promising alpha candidate: a **stateful two-stage detector** (sweep
  arms a pending setup for K bars; enter on MSS while armed; FVG still
  required) — converts "MSS on this exact bar" to "MSS within K bars",
  roughly an order of magnitude more chains to evaluate.
- ~~15m needs a different exit design…~~ **See the correction below — the
  15m finding was retracted.**
- Re-run: `python backtest.py --days 60 --slippage-bps 2` (and 15m via the
  same harness) after market regime shifts or checklist changes.

### Sep 25, 2026 (later) — data-integrity correction: 15m finding retracted

While validating the two-stage detector, the earlier results failed a
cross-check: the same strict configuration produced 2, 79, and 128 signals
depending on which harness ran it. Root cause: the cached dataset used for
the 15m/60-day studies was a **continuous per-symbol index** spanning all
44 days, so the 46-bar context window at each 9:30 open contained **the
prior session's late-day bars**. The detector then read the overnight gap
against yesterday's range as a "sweep + MSS + FVG" — i.e. the 41–42
"15m trades" were mostly **gap-continuation artifacts**, not TJR liquidity
sequences. The strict checklist never fired at all on 15m bars once context
was correctly confined to the current session (0 setups, 528 symbol-days,
both windows).

**Protocol fix (now the standard):** datasets must be sliced per symbol-day
(07:00–16:00 ET) *before* resampling and *before* windowing, matching the
live scanner's per-run context; caches are verified frame-by-frame against
the validated `_fetch_day` path (byte-equality on sampled days) before any
study runs.

**Re-run results on the verified dataset (5m, extended 09:30–15:55,
44 sessions × 12 symbols, 2bps slippage):**

| arm (SWEEP_ARM_BARS) | raw signals | trades | win% | avg R | PF | P&L |
|---|---|---|---|---|---|---|
| 5 (strict baseline) | 8 | 8 | 62% | −0.19 | 1.66 | +$6 |
| 8 | 11 | 10 | 50% | −0.26 | 1.64 | +$8 |
| 12 | 12 | 11 | 27% | −0.61 | 0.27 | −$17 |
| 15 | 4 | 3 | 67% | −0.33 | 1.93 | +$3 |

(All counts are tiny — 3–11 trades — so none of these PFs is meaningful;
the arm=15 dip is partly an artifact of `min_bars` growing with the arm,
which pushes the first evaluable bar later into the session. Several exits
are `EOD-DATA`: the data slice ends before the flatten bar prints.)

**Corrected conclusions**
1. On 5m bars the strict sequence is genuinely rare (~0.015 signals /
   symbol-day extended; ~0 in the 9:30–11:00 window) — the original 5m
   conclusion stands.
2. **The 15m "frequency fix" was an artifact.** On correct per-session
   context, 15m produces no setups at all. The timeframe hypothesis is
   dead as tested.
3. Two-stage arming (5→15 bars) does **not** demonstrate an edge on this
   sample: frequency rises slightly, quality does not. The knob ships
   (`SWEEP_ARM_BARS`, default 5 = strict baseline) for future re-tests,
   but the strict default stays.
4. The earlier PF 2.06 / +$246 arm=5 result is likewise retracted — it was
   the same cross-session leakage in serialized-trade form.
5. Every future study must use the per-day slice + byte-verification
   protocol before results are recorded here.

## Known deviations from the original spec

- `yfinance` / `smartmoneyconcepts` are listed per spec but currently unused
  (the SMC math is implemented deterministically in `strategy.py`). Safe to
  prune if you want a leaner install.
- The workflow uses the `timezone:` schedule field (GA March 2026). If your
  runner predates it, switch the cron to UTC: `*/5 13-14 * * 1-5` + `0 15`.
- Gemini's MCP tool-loop requires the async client (`client.aio`) — handled.
- Long-only in practice: short entries are signalled but the agentic
  account's borrow path is unverified; exit math mirrors for shorts when
  enabled.

## Robinhood Trading MCP — one-time auth bootstrap

The official endpoint (`agent.robinhood.com/mcp/trading`) is OAuth-protected
and trades only inside a dedicated **Agentic account** you open during
consent (desktop browser required by Robinhood).

```bash
python robinhood_auth.py   # opens browser → approve → consent
```

That saves tokens locally (~/.tjr_bot/robinhood_tokens.json), proves the
connection by listing the MCP tools, and prints the JSON blob to paste as
the `ROBINHOOD_TOKEN_STORE_JSON` GitHub secret (or Render env var if using
the optional HTTP wrapper). Refreshes are automatic at runtime; re-run the
bootstrap only if consent is revoked.

## ⚠️ Risk notes (read the vault's 03 Risk Register)

- Robinhood MCP automation sits in a ToS gray zone — keep size small; the
  account (not you) bears restriction risk. Verify stop-loss attachment
  support before scaling beyond Phase 2.
- Paper-trade first: the workflow defaults to `EXECUTOR_DRY_RUN=1` (real
  scans, simulated fills). Flip to `0` deliberately.
- Nothing here is financial advice.
