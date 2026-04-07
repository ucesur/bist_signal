# CLAUDE.md — BIST Signal Bot

This file is the primary reference for understanding and modifying this codebase.
Read it fully before making any changes.

---

## Project overview

Single-file Python bot (`bist_signal_bot.py`) that monitors BIST (Borsa Istanbul)
stocks, generates technical BUY/SELL/WAIT signals, and sends alerts via Telegram
and email. It also runs a paper-trading simulation with a 10,000 TRY starting
balance and delivers an end-of-day performance report.

**Current version:** v2.1  
**Python:** 3.8+  
**Dependencies:** `requests`, `schedule`, `python-dotenv` (stdlib only otherwise)

```
pip install requests schedule python-dotenv
```

---

## Running the bot

```bash
python bist_signal_bot.py
```

The bot:
1. Creates `stocks/` folder and sample `.txt` files if missing
2. Sleeps until 10:00 if market is not yet open
3. Scans all symbols every `SCAN_INTERVAL_MIN` minutes (default 10)
4. Sends Telegram + email alerts on signal changes
5. Sends end-of-day report when session closes (18:00)

---

## File structure

```
bist_signal_bot.py   ← entire bot, single file
stocks/              ← one .txt file per stock symbol (auto-created)
  KCAER.txt
  ECILC.txt
  TTRAK.txt
.env                 ← credentials and tuning parameters (never commit)
bist.log             ← rotating log, cleared on startup
CLAUDE.md            ← this file
```

---

## Adding a stock

Create `stocks/SYMBOL.txt`. The bot picks it up on the next scan with no restart.

```ini
# KCAER.txt — Kocaer Steel
name              = Kocaer Steel
strong_support    = 11.00
mid_support       = 11.80
resistance_1      = 12.20
resistance_2      = 12.60
resistance_3      = 14.05
stop_pct          = 0.04        # 4% stop-loss from avg cost
volume_multiplier = 1.5         # volume must be 1.5x avg to confirm signal

# trend and trend_strength are OPTIONAL.
# If omitted, trend is auto-detected via EMA crossover every scan.
# Set manually here to override the auto-detection permanently:
# trend             = up         # up | down | sideways
# trend_strength    = strong     # strong | weak
```

**Required fields:** `name`, `strong_support`, `mid_support`, `resistance_1`,
`resistance_2`, `resistance_3`, `stop_pct`, `volume_multiplier`

**Optional fields:** `trend`, `trend_strength` (default: auto-detected)

---

## .env reference

Copy this block into `.env` and fill in your values:

```dotenv
# ── Notifications ──────────────────────────────────────────────
TELEGRAM_TOKEN    = 123456:ABCdef...
TELEGRAM_CHAT_ID  = -1001234567890
TELEGRAM_AKTIF    = true

GMAIL_GONDEREN    = yourbot@gmail.com
GMAIL_SIFRE       = xxxx-xxxx-xxxx-xxxx   # App Password, not account password
GMAIL_ALICI       = you@example.com
EMAIL_AKTIF       = true

# ── Stocks & session ───────────────────────────────────────────
HISSELER_KLASOR   = stocks
SCAN_INTERVAL_MIN = 10         # minutes between scans
VOL_WARMUP_SCANS  = 5          # ignore volume signals until N unique readings

# ── Technical estimation (v2.0) ────────────────────────────────
RSI_PERIOD        = 14         # RSI lookback in scans; lower = faster but noisier
MOMENTUM_PERIOD   = 5          # rate-of-change lookback (~50 min at 10-min scans)
EMA_SHORT         = 5          # fast EMA for trend detection
EMA_LONG          = 20         # slow EMA for trend detection
PRICE_WINDOW      = 50         # max price history readings per symbol

# ── DCA — Dollar-Cost Averaging (v2.1) ─────────────────────────
DCA_ENABLED       = true
DCA_MAX_TRANCHES  = 2          # total buys per symbol including the first (hard cap: 5)
DCA_MIN_DROP_PCT  = 3.0        # min % drop from last tranche price to add another
DCA_RSI_MIN       = 28         # block DCA if RSI < this (price in freefall)

# ── Logging ────────────────────────────────────────────────────
LOG_FILE          = bist.log
LOG_MAX_MB        = 10
LOG_BACKUPS       = 7
LOG_LEVEL         = INFO       # INFO or DEBUG
```

---

## Architecture — code sections in order

| Lines (approx) | Section | Purpose |
|---|---|---|
| 1–41 | Docstring | Version history and parameter reference |
| 42–88 | Logging | Console + rotating file handler setup |
| 90–119 | Settings | All constants loaded from `.env` |
| 121–197 | Stock loading | `load_stock()`, `scan_stocks()` — reads `.txt` files |
| 200–261 | Sample files | `SAMPLE_STOCKS`, `create_sample_files()` |
| 263–553 | Portfolio simulation | Dataclasses, buy/sell/DCA functions |
| 556–648 | Data fetching | `fetch_price()` via Bigpara API |
| 650–740 | Technical indicators | RSI, EMA, trend detection, momentum, confidence |
| 742–880 | Signal engine | `Signal` dataclass, `generate_signal()` |
| 882–980 | Notifications | Message builders, Telegram, email |
| 982–1090 | EOD report | `_build_eod_report()`, email/telegram senders |
| 1092–1130 | Session check | `session_open()`, signal change detection |
| 1132–1220 | Main loop | `scan()` — orchestrates everything |
| 1222–1337 | Entry point | Startup banner, sleep-until-open, scheduler |

---

## Data flow — one scan cycle

```
scan()
  └─ scan_stocks()              reads stocks/ folder → dict of symbol configs
  └─ for each symbol:
       fetch_price()            Bigpara API → price, volume, change
         └─ _update_volume_avg()   rolling 20-reading volume average
         └─ _update_price_history() rolling 50-reading price buffer
       generate_signal()        technical analysis → Signal dataclass
         └─ compute_rsi()       14-scan RSI from price history
         └─ compute_momentum()  5-scan rate-of-change %
         └─ detect_trend()      EMA5 vs EMA20 crossover (or manual override)
         └─ compute_confidence() 0-100 score from all four indicators
       check_stop_loss()        triggers sell if price ≤ avg_cost × (1 - stop_pct)
       updateBalance()          routes signal to portfolio action
         ├─ no position  → portfolio_buy()          (first tranche)
         ├─ has position → _dca_eligible()?
         │    yes        → portfolio_add_tranche()  (DCA)
         │    no         → skip
         └─ SELL signal  → portfolio_sell()         (all tranches)
       send_alert() / send_dca_alert()   Telegram + email
```

---

## Key dataclasses

### `Tranche`
A single buy event. Stored inside `Position.tranches`.
```python
@dataclass
class Tranche:
    price:    float   # buy price of this tranche
    quantity: int     # shares bought
    time:     str     # "HH:MM"
```

### `Position`
Holds one or more tranches for a symbol. All cost/quantity properties are
computed from `tranches` — never stored redundantly.
```python
pos.quantity           # total shares across all tranches
pos.avg_price          # weighted average cost
pos.last_tranche_price # price of the most recent tranche (DCA drop reference)
pos.tranche_count      # how many tranches have been added
pos.total_cost         # total capital deployed (excl. commission)
pos.buy_price          # alias for avg_price (used by stop-loss)
```

### `Signal`
Output of `generate_signal()`. Read-only after creation.
```python
signal.side        # "BUY" | "SELL" | "WAIT"
signal.strength    # "STRONG" | "NORMAL" | "BREAKOUT" | "TAKE PROFIT" | "NEUTRAL"
signal.rsi         # float or None (warmup period)
signal.momentum    # float % or None
signal.auto_trend  # "up" | "down" | "sideways"
signal.trend_source # "ema" | "manual" | "default"
signal.confidence  # 0–100 int
```

---

## Signal logic — when each signal fires

| Price condition | Volume | Trend | Signal |
|---|---|---|---|
| `price ≤ strong_support` | any | not strong down | BUY STRONG |
| `price ≤ strong_support` | any | strong down | WAIT (falling knife) |
| `price ≤ mid_support` | high | not down | BUY NORMAL |
| `price ≤ mid_support` | high | down | WAIT |
| `price > resistance_1` | high | not down | BUY BREAKOUT |
| `price ≥ resistance_2` | any | strong up + RSI < 75 | WAIT (hold for R3) |
| `price ≥ resistance_2` | any | otherwise | SELL TAKE PROFIT |
| `price ≥ resistance_3` | any | any | SELL TAKE PROFIT |

Volume is considered "high" when `volume > avg_vol × volume_multiplier`
AND `vol_warmup_count ≥ VOL_WARMUP_SCANS`.

---

## DCA logic

DCA (Dollar-Cost Averaging) adds a new tranche to an existing position when the
price drops enough and the estimation still points upward.

**All six conditions must be true:**

| # | Condition | Why |
|---|---|---|
| 1 | `DCA_ENABLED = true` | Global on/off switch |
| 2 | Position exists for symbol | Nothing to DCA into if not holding |
| 3 | `tranche_count < DCA_MAX_TRANCHES` | Prevents infinite averaging |
| 4 | `drop_pct ≥ DCA_MIN_DROP_PCT` | Must be a meaningful dip, not noise |
| 5 | `trend != "down"` (unless source is "default") | Estimation must be upward |
| 6 | `RSI ≥ DCA_RSI_MIN` or RSI is None | Not buying into freefall |

`drop_pct` is measured from `last_tranche_price`, not from `avg_price`.
Each subsequent DCA tranche is measured from the previous one.

**Stop-loss after DCA:** Always calculated from `avg_price`, not the first buy.
Example: buy at 12.20, DCA at 11.60 → avg ≈ 11.90 → stop at 11.42 (4%).

**Each tranche uses `POSITION_SIZE_PCT × current_cash`**, so later tranches are
smaller in absolute TRY terms as cash decreases.

---

## Confidence score (0–100)

Four components, each contributing up to 20–25 points:

| Component | Max | BUY earns max when | SELL earns max when |
|---|---|---|---|
| Volume | 20 | `volume_ok = True` | `volume_ok = True` |
| RSI | 20 | RSI < 30 (oversold) | RSI > 70 (overbought) |
| Trend | 20 | trend = "up" | trend = "down" |
| Momentum | 20 | momentum < −0.5% (dip) | momentum > +0.5% (push) |

Score labels: 0–24 VERY LOW · 25–49 LOW · 50–74 MODERATE · 75–100 HIGH

---

## Bigpara API

Endpoint: `https://bigpara.hurriyet.com.tr/api/v1/borsa/hisseyuzeysel/{SYMBOL}`

Data used:
- `alis` (bid price) — used during session hours
- `kapanis` (last close) — used outside session hours
- `hacimtl` (TRY volume) — parsed via `_parse_volume()`
- `yuzdedegisim` (% change) — stored but not used in signals

The bot adds a 0.3–1.2s random jitter before each request to avoid 401
rate-limiting. On 401/network error it retries up to 3 times (3s, 6s apart).
Volume deduplication skips identical consecutive values to prevent inflating the
rolling average during Bigpara throttle periods.

---

## Trade types in trades list

| `side` value | Meaning |
|---|---|
| `"BUY"` | First tranche — new position opened |
| `"DCA"` | Additional tranche — added to existing position |
| `"SELL"` | Full position closed (all tranches at once) |

---

## Notification messages

**Regular signal alert** — sent via `send_alert(signal)`:
- Price, signal side/strength, volume status
- RSI, momentum, trend, confidence bar
- Stop-loss and targets
- DCA status line if position is open (tranche count + remaining)

**DCA alert** — sent via `send_dca_alert()` after a tranche is added:
- Tranche number, drop %, new avg cost, total quantity
- Per-tranche breakdown (price, quantity, time)
- RSI and trend at time of DCA

**End-of-day report** — sent when session closes (18:00):
- Portfolio total, P&L %, open positions with live RSI/momentum
- Per-position tranche breakdown with individual P&L
- Full trade log (BUY / DCA / SELL)

---

## Important implementation rules

1. **Never use `pos.buy_price` to calculate stop-loss** — always use `pos.avg_price`.
   The `buy_price` property is an alias for `avg_price` and exists only for
   backward compatibility with any code that references it by name.

2. **`position.quantity` is computed, not stored.** Do not add a `quantity` field
   to `Position` — it would diverge from the tranches list.

3. **`signal_changed()` only compares `side` and `strength`**, not price or
   confidence. This prevents alert spam when the price wiggles inside the same
   signal zone.

4. **DCA does not re-trigger `send_alert()`** — it has its own `send_dca_alert()`
   function with a dedicated message format showing the tranche breakdown.

5. **`_price_history` is per-symbol, session-persistent.** It is cleared when a
   symbol is removed from the `stocks/` folder. It is NOT cleared at end-of-day
   so indicators stay warm across day-start warmup.

6. **Commission is 0.1% per trade** (`COMMISSION_RATE = 0.001`), applied on both
   buy and sell. DCA trades also pay commission.

7. **`portfolio_sell()` calculates P&L as:**
   `net_proceeds − total_cost − buy_commissions`
   where `total_cost = Σ(tranche.price × tranche.quantity)`.

---

## Common modifications

**Change scan interval to 2 minutes:**
```dotenv
SCAN_INTERVAL_MIN = 2
```

**Allow up to 3 DCA tranches, trigger on 5% drops only:**
```dotenv
DCA_MAX_TRANCHES = 3
DCA_MIN_DROP_PCT = 5.0
```

**Tighten trend detection (detect weaker trends):**
```dotenv
EMA_SHORT = 3
EMA_LONG  = 10
```
Note: the `diff_pct` thresholds (1.5% / 3.0%) inside `detect_trend()` are
hardcoded. If you lower EMA periods significantly, also lower those thresholds.

**Disable DCA without touching code:**
```dotenv
DCA_ENABLED = false
```

**Run in debug mode to see all DCA eligibility checks:**
```dotenv
LOG_LEVEL = DEBUG
```

---

## Version history

| Version | Changes |
|---|---|
| v1.7 | Original — dynamic stock loading from `.txt` files, Bigpara API, Telegram + email |
| v2.0 | RSI, momentum, EMA auto-trend, confidence score, price history buffer |
| v2.1 | DCA (Dollar-Cost Averaging) — multi-tranche positions, weighted avg cost, dedicated DCA alerts |

---

## What is NOT in this bot

- Real order execution — simulation only
- News / sentiment analysis (was v3.0, cancelled)
- Multi-day position persistence — portfolio resets on process restart
- Database or file-based state — everything lives in memory
- Backtesting — the bot only operates on live/delayed data
