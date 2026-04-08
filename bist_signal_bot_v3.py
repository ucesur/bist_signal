"""
BIST Stock Signal Bot
======================
Stocks     : Auto-loaded from .txt files in the stocks/ folder
Interval   : 10 minutes (schedule)
Alerts     : Telegram + Gmail (smtplib)
Simulation : 10,000 TRY starting balance, end-of-day email report

Data providers (v2.2) — choose one in .env:
  PRICE_PROVIDER = bigpara   (default — no extra install needed, ~15 min delayed)
  PRICE_PROVIDER = yfinance  (Yahoo Finance chart API — no extra install needed)

  Bigpara pros  : Turkish UI, TRY-denominated volume figures
  Bigpara cons  : Throttles under load and returns cached/stale prices (401 fallback)
  yfinance pros : More stable, independent source, no extra dependency (uses requests)
                  Provides full day of 1-min OHLCV bars — RSI/EMA warm up instantly
  yfinance cons : Volume is share count (not TRY) — recalibrate volume_multiplier

Estimation (v2.0):
  - RSI          : 14-scan RSI, oversold/overbought zones
  - Momentum     : 5-scan rate-of-change %
  - Auto trend   : EMA5 vs EMA20 crossover (replaces manual trend field)
  - Confidence   : 0-100 score (volume + RSI + trend + momentum)

DCA — Dollar-Cost Averaging (v2.1):
  - When a held stock drops enough, the bot buys another tranche at the cheaper price
  - Estimation must point upward: trend must be 'up' or 'sideways', RSI must not be in freefall
  - Each position tracks individual tranches, average cost, and total shares
  - Stop-loss is calculated from the weighted average price across all tranches

Broker Edition (v3.0):
  - Trailing stop-loss    : locks in profit as price rises
  - Partial profit taking : sell 50% at target 1, hold rest for target 2/3
  - Confidence gate       : only enter trades above MIN_CONFIDENCE
  - Session time filter   : skip noisy first/last 30 min of session
  - Dynamic position size : scale allocation by confidence score
  - Risk/Reward filter    : skip trades with R:R < 2:1
  - Breakeven stop        : move stop to entry after +1.5% gain
  - Cooldown timer        : no re-entry within N minutes after a stop-loss

New .env parameters (v3.0):
  TRAILING_STOP_ENABLED = true
  TRAILING_STOP_PCT     = 2.0         # trail by 2% from high watermark
  PARTIAL_PROFIT_PCT    = 50          # sell 50% at target 1
  MIN_CONFIDENCE        = 40          # skip signals below this
  AVOID_OPEN_MINUTES    = 30          # skip first N min after open
  AVOID_CLOSE_MINUTES   = 30          # skip last N min before close
  DYNAMIC_SIZING        = true        # scale position by confidence
  MIN_RISK_REWARD       = 2.0         # minimum R:R ratio to enter
  BREAKEVEN_TRIGGER_PCT = 1.5         # move stop to entry after this gain
  STOPLOSS_COOLDOWN_MIN = 60          # no re-buy for N min after stop-loss

.env parameters (v2.2):
  PRICE_PROVIDER    = bigpara   (or yfinance)

.env parameters (v2.1):
  DCA_ENABLED       = true
  DCA_MAX_TRANCHES  = 2
  DCA_MIN_DROP_PCT  = 3.0
  DCA_RSI_MIN       = 28

.env parameters (v2.0):
  RSI_PERIOD      = 14
  MOMENTUM_PERIOD = 5
  EMA_SHORT       = 5
  EMA_LONG        = 20
  PRICE_WINDOW    = 50

To add a new stock:
  Create a stocks/SYMBOL.txt file — the bot picks it up on the next scan.
  trend / trend_strength are optional — auto-detected via EMA crossover.

Setup:
  pip install requests schedule python-dotenv
  (No extra install needed for either provider — both use requests)
"""

import os
import glob
import requests
import schedule
import smtplib
import time
import logging
import logging.handlers
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────
#  LOGGING — Console + Rotating File
# ─────────────────────────────────────────

LOG_FILE     = os.getenv("LOG_FILE",   "bist.log")
LOG_MAX_MB   = int(os.getenv("LOG_MAX_MB",  "10"))
LOG_BACKUPS  = int(os.getenv("LOG_BACKUPS", "7"))
LOG_LEVEL    = os.getenv("LOG_LEVEL",  "INFO").upper()

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")

_console = logging.StreamHandler()
_console.setFormatter(_fmt)
_console.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

open(LOG_FILE, "w").close()

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE,
    maxBytes=LOG_MAX_MB * 1024 * 1024,
    backupCount=LOG_BACKUPS,
    encoding="utf-8",
)
_file_handler.setFormatter(_fmt)
_file_handler.setLevel(logging.DEBUG)

logging.basicConfig(level=logging.DEBUG, handlers=[_console, _file_handler])
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
#  SETTINGS
# ─────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GMAIL_SENDER     = os.getenv("GMAIL_GONDEREN",   "")
GMAIL_PASSWORD   = os.getenv("GMAIL_SIFRE",      "")
GMAIL_RECIPIENT  = os.getenv("GMAIL_ALICI",      "")
TELEGRAM_ENABLED = os.getenv("TELEGRAM_AKTIF", "true").lower() == "true"
EMAIL_ENABLED    = os.getenv("EMAIL_AKTIF",    "true").lower() == "true"

STOCKS_FOLDER    = os.getenv("HISSELER_KLASOR", "stocks")
SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL_MIN", "10"))
VOL_WARMUP_SCANS = int(os.getenv("VOL_WARMUP_SCANS",  "5"))
SESSION_START    = 10
SESSION_END      = 18
COMMISSION_RATE  = 0.001

# ── Technical estimation (v2.0) ──────────────────────
RSI_PERIOD      = int(os.getenv("RSI_PERIOD",      "14"))
MOMENTUM_PERIOD = int(os.getenv("MOMENTUM_PERIOD", "5"))
EMA_SHORT       = int(os.getenv("EMA_SHORT",       "5"))
EMA_LONG        = int(os.getenv("EMA_LONG",        "20"))
PRICE_WINDOW    = int(os.getenv("PRICE_WINDOW",    "50"))

# ── DCA parameters (v2.1) ────────────────────────────
DCA_ENABLED      = os.getenv("DCA_ENABLED",  "true").lower() == "true"
DCA_MAX_TRANCHES = min(int(os.getenv("DCA_MAX_TRANCHES", "2")), 5)   # hard cap at 5
DCA_MIN_DROP_PCT = float(os.getenv("DCA_MIN_DROP_PCT", "3.0"))        # % drop from last tranche
DCA_RSI_MIN      = float(os.getenv("DCA_RSI_MIN",      "28"))         # don't DCA in freefall

# ── Broker Edition parameters (v3.0) ────────────────
TRAILING_STOP_ENABLED  = os.getenv("TRAILING_STOP_ENABLED", "true").lower() == "true"
TRAILING_STOP_PCT      = float(os.getenv("TRAILING_STOP_PCT",     "2.0"))   # % from high
PARTIAL_PROFIT_PCT     = int(os.getenv("PARTIAL_PROFIT_PCT",      "50"))    # % to sell at T1
MIN_CONFIDENCE         = int(os.getenv("MIN_CONFIDENCE",          "40"))    # gate
AVOID_OPEN_MINUTES     = int(os.getenv("AVOID_OPEN_MINUTES",     "30"))    # skip first N min
AVOID_CLOSE_MINUTES    = int(os.getenv("AVOID_CLOSE_MINUTES",    "30"))    # skip last N min
DYNAMIC_SIZING         = os.getenv("DYNAMIC_SIZING", "true").lower() == "true"
MIN_RISK_REWARD        = float(os.getenv("MIN_RISK_REWARD",      "2.0"))
BREAKEVEN_TRIGGER_PCT  = float(os.getenv("BREAKEVEN_TRIGGER_PCT","1.5"))
STOPLOSS_COOLDOWN_MIN  = int(os.getenv("STOPLOSS_COOLDOWN_MIN", "60"))

# ── Price provider (v2.2) ────────────────────────────
_RAW_PROVIDER = os.getenv("PRICE_PROVIDER", "bigpara").strip().lower()
if _RAW_PROVIDER not in ("bigpara", "yfinance"):
    print(f"[WARN] Unknown PRICE_PROVIDER={_RAW_PROVIDER!r}, falling back to bigpara.")
    _RAW_PROVIDER = "bigpara"
PRICE_PROVIDER: str = _RAW_PROVIDER   # "bigpara" | "yfinance"

# ─────────────────────────────────────────
#  STOCK LOADING — FROM TXT FILES
# ─────────────────────────────────────────

REQUIRED_FIELDS = [
    "name", "strong_support", "mid_support",
    "resistance_1", "resistance_2", "resistance_3",
    "stop_pct", "volume_multiplier",
]
STRING_FIELDS = {"name", "trend", "trend_strength"}


def load_stock(file_path: str) -> Optional[tuple]:
    """
    Reads a single .txt file and converts it to a stock dictionary.
    Returns (symbol, data_dict) or None on error.

    File format (SYMBOL.txt):
        name              = Kocaer Steel
        strong_support    = 11.00
        mid_support       = 11.80
        resistance_1      = 12.20
        resistance_2      = 12.60
        resistance_3      = 14.05
        stop_pct          = 0.04
        volume_multiplier = 1.5
        # trend / trend_strength optional — auto-detected via EMA crossover
    """
    symbol = os.path.splitext(os.path.basename(file_path))[0].upper()
    data   = {}
    try:
        with open(file_path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    log.warning(f"{file_path}:{line_no} — '=' not found, skipped: {line!r}")
                    continue
                key, _, value = line.partition("=")
                key   = key.strip().lower()
                value = value.strip()
                if key in STRING_FIELDS:
                    data[key] = value.lower()
                else:
                    try:
                        data[key] = float(value.replace(",", "."))
                    except ValueError:
                        log.warning(f"{file_path}:{line_no} — '{key}' bad float: {value!r}")
                        return None

        missing = [f for f in REQUIRED_FIELDS if f not in data]
        if missing:
            log.error(f"{file_path} — Missing fields: {missing}")
            return None

        data.setdefault("trend",          None)
        data.setdefault("trend_strength", None)
        return symbol, data

    except FileNotFoundError:
        log.error(f"{file_path} — File not found.")
        return None
    except Exception as e:
        log.error(f"{file_path} — Read error: {e}")
        return None


def scan_stocks() -> dict:
    if not os.path.isdir(STOCKS_FOLDER):
        os.makedirs(STOCKS_FOLDER)
        log.info(f"'{STOCKS_FOLDER}/' folder created.")
    files  = glob.glob(os.path.join(STOCKS_FOLDER, "*.txt"))
    stocks = {}
    for file in sorted(files):
        result = load_stock(file)
        if result:
            stocks[result[0]] = result[1]
    return stocks


# ─────────────────────────────────────────
#  SAMPLE FILES
# ─────────────────────────────────────────

SAMPLE_STOCKS = {
    "KCAER.txt": """\
# Kocaer Steel — Technical Levels
# trend / trend_strength optional — auto-detected by EMA crossover.

name              = Kocaer Steel
strong_support    = 11.00
mid_support       = 11.80
resistance_1      = 12.20
resistance_2      = 12.60
resistance_3      = 14.05
stop_pct          = 0.04
volume_multiplier = 1.5
""",
    "ECILC.txt": """\
# Eczacibasi Pharma — Technical Levels

name              = Eczacibasi Pharma
strong_support    = 112.00
mid_support       = 114.00
resistance_1      = 117.00
resistance_2      = 120.00
resistance_3      = 128.00
stop_pct          = 0.03
volume_multiplier = 1.5
""",
    "TTRAK.txt": """\
# Turk Traktor — Technical Levels

name              = Turk Traktor
strong_support    = 440.00
mid_support       = 460.00
resistance_1      = 480.00
resistance_2      = 502.50
resistance_3      = 575.00
stop_pct          = 0.04
volume_multiplier = 1.5
""",
}


def create_sample_files():
    if not os.path.isdir(STOCKS_FOLDER):
        os.makedirs(STOCKS_FOLDER)
    if glob.glob(os.path.join(STOCKS_FOLDER, "*.txt")):
        return
    for filename, content in SAMPLE_STOCKS.items():
        path = os.path.join(STOCKS_FOLDER, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        log.info(f"Sample file created: {path}")


# ─────────────────────────────────────────
#  SIMULATION — PORTFOLIO STATE
# ─────────────────────────────────────────

@dataclass
class Tranche:
    """A single buy at a specific price and time."""
    price:    float
    quantity: int
    time:     str


@dataclass
class Position:
    """
    Holds one or more tranches for the same symbol.
    All cost/quantity properties are computed from tranches so they
    stay accurate after each DCA add.
    """
    symbol:   str
    name:     str
    tranches: list = field(default_factory=list)   # list[Tranche]

    @property
    def quantity(self) -> int:
        return sum(t.quantity for t in self.tranches)

    @property
    def avg_price(self) -> float:
        """Weighted average cost across all tranches."""
        total_qty = self.quantity
        if total_qty == 0:
            return 0.0
        return round(sum(t.price * t.quantity for t in self.tranches) / total_qty, 4)

    @property
    def buy_price(self) -> float:
        """Alias for avg_price — used by stop-loss check."""
        return self.avg_price

    @property
    def buy_time(self) -> str:
        return self.tranches[0].time if self.tranches else ""

    @property
    def last_tranche_price(self) -> float:
        return self.tranches[-1].price if self.tranches else 0.0

    @property
    def tranche_count(self) -> int:
        return len(self.tranches)

    @property
    def total_cost(self) -> float:
        """Total capital deployed (excluding commission)."""
        return sum(t.price * t.quantity for t in self.tranches)


@dataclass
class Trade:
    time:       str
    symbol:     str
    side:       str       # BUY / DCA / SELL
    price:      float
    quantity:   int
    amount:     float
    commission: float
    pnl:        Optional[float]
    reason:     str


@dataclass
class Portfolio:
    starting_balance:  float = 10_000.0
    cash:              float = 10_000.0
    positions:         dict  = field(default_factory=dict)   # symbol → Position
    trades:            list  = field(default_factory=list)   # list[Trade]
    day_start:         str   = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    POSITION_SIZE_PCT: float = 0.30

    def total_value(self, current_prices: dict) -> float:
        return self.cash + sum(
            pos.quantity * current_prices.get(sym, pos.avg_price)
            for sym, pos in self.positions.items()
        )

    def total_pnl(self, current_prices: dict) -> float:
        return self.total_value(current_prices) - self.starting_balance

    def pnl_pct(self, current_prices: dict) -> float:
        return self.total_pnl(current_prices) / self.starting_balance * 100


portfolio        = Portfolio()
_current_prices: dict = {}

# ── Broker v3.0: trailing stops & cooldowns ──────────
_trailing_highs: dict = {}      # symbol → highest price since entry
_stoploss_cooldown: dict = {}   # symbol → datetime of last stop-loss hit


def _in_cooldown(symbol: str) -> bool:
    """Returns True if symbol was stopped out recently."""
    if symbol not in _stoploss_cooldown:
        return False
    elapsed = (datetime.now() - _stoploss_cooldown[symbol]).total_seconds() / 60
    if elapsed < STOPLOSS_COOLDOWN_MIN:
        log.debug(f"[COOLDOWN] {symbol}: {STOPLOSS_COOLDOWN_MIN - elapsed:.0f} min remaining")
        return True
    del _stoploss_cooldown[symbol]
    return False


def _in_quiet_zone() -> bool:
    """Returns True during noisy open/close windows."""
    now = datetime.now()
    minutes_since_open = (now.hour - SESSION_START) * 60 + now.minute
    minutes_until_close = (SESSION_END - now.hour) * 60 - now.minute
    if minutes_since_open < AVOID_OPEN_MINUTES:
        log.debug(f"[TIME] Quiet zone: {AVOID_OPEN_MINUTES - minutes_since_open} min until entries allowed")
        return True
    if minutes_until_close < AVOID_CLOSE_MINUTES:
        log.debug(f"[TIME] Quiet zone: close in {minutes_until_close} min — no new entries")
        return True
    return False


def _calc_risk_reward(price: float, stop: float, target: float) -> float:
    """Returns reward:risk ratio. Higher is better."""
    risk = abs(price - stop)
    reward = abs(target - price)
    if risk == 0:
        return 0.0
    return round(reward / risk, 2)


def _dynamic_position_pct(confidence: int) -> float:
    """Scale position size by confidence: 15%-40% of cash."""
    if not DYNAMIC_SIZING:
        return portfolio.POSITION_SIZE_PCT
    # Linear scale: conf 40→15%, conf 80→40%
    base = 0.15
    scale = min(max(confidence - 40, 0), 40) / 40  # 0.0–1.0
    return base + scale * 0.25


def portfolio_buy(symbol: str, price: float, reason: str, stocks: dict,
                  confidence: int = 50) -> bool:
    """Opens a new position (first tranche). Skips if already in position."""
    if symbol in portfolio.positions:
        log.info(f"[SIM] {symbol}: Position already open — use DCA to add.")
        return False

    pct = _dynamic_position_pct(confidence)
    allocate = portfolio.cash * pct
    if allocate < price:
        log.info(f"[SIM] {symbol}: Insufficient cash ({portfolio.cash:.2f} TRY).")
        return False

    quantity = int(allocate / price)
    if quantity == 0:
        return False

    amount     = quantity * price
    commission = amount * COMMISSION_RATE
    portfolio.cash -= (amount + commission)

    tranche  = Tranche(price=price, quantity=quantity,
                       time=datetime.now().strftime("%H:%M"))
    portfolio.positions[symbol] = Position(
        symbol=symbol, name=stocks[symbol]["name"], tranches=[tranche]
    )
    portfolio.trades.append(Trade(
        time=datetime.now().strftime("%H:%M"), symbol=symbol,
        side="BUY", price=price, quantity=quantity, amount=amount,
        commission=commission, pnl=None, reason=reason,
    ))
    log.info(
        f"[SIM] BUY  | {symbol} | {quantity} @ {price} TRY "
        f"| alloc={pct*100:.0f}% (conf={confidence}) | Cash: {portfolio.cash:.2f} TRY"
    )
    _trailing_highs[symbol] = price   # initialize trailing stop tracker
    return True


def portfolio_add_tranche(symbol: str, price: float, reason: str) -> bool:
    """
    Adds another tranche to an existing position (DCA buy).
    Uses the same POSITION_SIZE_PCT of current cash.
    Returns False if position doesn't exist or cash is insufficient.
    """
    pos = portfolio.positions.get(symbol)
    if not pos:
        return False

    allocate = portfolio.cash * portfolio.POSITION_SIZE_PCT
    if allocate < price:
        log.info(f"[SIM] {symbol}: Insufficient cash for DCA ({portfolio.cash:.2f} TRY).")
        return False

    quantity = int(allocate / price)
    if quantity == 0:
        return False

    amount     = quantity * price
    commission = amount * COMMISSION_RATE
    portfolio.cash -= (amount + commission)

    tranche = Tranche(price=price, quantity=quantity,
                      time=datetime.now().strftime("%H:%M"))
    pos.tranches.append(tranche)

    portfolio.trades.append(Trade(
        time=datetime.now().strftime("%H:%M"), symbol=symbol,
        side="DCA", price=price, quantity=quantity, amount=amount,
        commission=commission, pnl=None, reason=reason,
    ))
    log.info(
        f"[SIM] DCA  | {symbol} | tranche #{pos.tranche_count} | "
        f"{quantity} @ {price} TRY | avg_cost={pos.avg_price:.4f} TRY "
        f"| total_qty={pos.quantity} | Cash: {portfolio.cash:.2f} TRY"
    )
    return True


def portfolio_sell(symbol: str, price: float, reason: str) -> bool:
    """Sells the full position (all tranches) at current price."""
    pos = portfolio.positions.get(symbol)
    if not pos:
        return False

    total_qty  = pos.quantity
    amount     = total_qty * price
    commission = amount * COMMISSION_RATE
    net        = amount - commission
    # P&L = net proceeds minus total invested cost (including buy commissions)
    buy_commission = pos.total_cost * COMMISSION_RATE
    pnl = net - pos.total_cost - buy_commission

    portfolio.cash += net
    del portfolio.positions[symbol]
    _trailing_highs.pop(symbol, None)   # clean up trailing tracker

    portfolio.trades.append(Trade(
        time=datetime.now().strftime("%H:%M"), symbol=symbol,
        side="SELL", price=price, quantity=total_qty, amount=amount,
        commission=commission, pnl=pnl, reason=reason,
    ))
    pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
    log.info(
        f"[SIM] SELL | {symbol} | {total_qty} @ {price} TRY "
        f"| avg_cost={pos.avg_price:.4f} | P&L: {pnl_str} TRY"
    )
    return True


def portfolio_sell_partial(symbol: str, price: float, pct: int, reason: str) -> bool:
    """Sells pct% of a position. If pct=100, sells all."""
    pos = portfolio.positions.get(symbol)
    if not pos:
        return False
    sell_qty = max(1, int(pos.quantity * pct / 100))
    if sell_qty >= pos.quantity:
        return portfolio_sell(symbol, price, reason)

    amount     = sell_qty * price
    commission = amount * COMMISSION_RATE
    net        = amount - commission
    # approximate partial P&L
    cost_basis = sell_qty * pos.avg_price
    buy_comm   = cost_basis * COMMISSION_RATE
    pnl        = net - cost_basis - buy_comm

    portfolio.cash += net
    # Remove shares from first tranche(s) proportionally
    remaining = sell_qty
    new_tranches = []
    for t in pos.tranches:
        if remaining <= 0:
            new_tranches.append(t)
        elif t.quantity <= remaining:
            remaining -= t.quantity
        else:
            new_tranches.append(Tranche(t.price, t.quantity - remaining, t.time))
            remaining = 0
    pos.tranches = new_tranches if new_tranches else pos.tranches

    portfolio.trades.append(Trade(
        time=datetime.now().strftime("%H:%M"), symbol=symbol,
        side="SELL", price=price, quantity=sell_qty, amount=amount,
        commission=commission, pnl=pnl, reason=reason,
    ))
    pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
    log.info(
        f"[SIM] PARTIAL SELL | {symbol} | {sell_qty}/{sell_qty + pos.quantity} "
        f"@ {price} TRY | P&L: {pnl_str} TRY | remaining: {pos.quantity}"
    )
    return True


def check_stop_loss(symbol: str, price: float, stocks: dict):
    """
    Broker v3.0: trailing stop + breakeven stop.
    - Updates high watermark
    - Moves stop to breakeven after BREAKEVEN_TRIGGER_PCT gain
    - Uses trailing stop from high watermark
    """
    pos = portfolio.positions.get(symbol)
    if not pos:
        return

    # Update trailing high
    if TRAILING_STOP_ENABLED:
        prev_high = _trailing_highs.get(symbol, price)
        if price > prev_high:
            _trailing_highs[symbol] = price
        high = _trailing_highs.get(symbol, price)

        # Breakeven stop: if price has risen BREAKEVEN_TRIGGER_PCT from avg cost,
        # the floor is the entry price (no loss allowed)
        gain_pct = (high - pos.avg_price) / pos.avg_price * 100
        if gain_pct >= BREAKEVEN_TRIGGER_PCT:
            breakeven_stop = pos.avg_price * 1.001  # tiny buffer above entry
            trailing_stop  = round(high * (1 - TRAILING_STOP_PCT / 100), 2)
            stop_price     = max(breakeven_stop, trailing_stop)
        else:
            stop_price = round(high * (1 - TRAILING_STOP_PCT / 100), 2)

        # Still respect the original fixed stop as absolute floor
        fixed_stop = round(pos.avg_price * (1 - stocks[symbol]["stop_pct"]), 2)
        # Use whichever is HIGHER (tighter) — but not below the fixed stop initially
        effective_stop = max(stop_price, fixed_stop) if gain_pct >= 0 else fixed_stop
    else:
        effective_stop = round(pos.avg_price * (1 - stocks[symbol]["stop_pct"]), 2)

    if price <= effective_stop:
        log.warning(
            f"[SIM] STOP-LOSS triggered! {symbol} @ {price} TRY "
            f"(avg_cost={pos.avg_price:.4f}, stop={effective_stop} TRY"
            + (f", trail_high={_trailing_highs.get(symbol, 0):.2f}" if TRAILING_STOP_ENABLED else "")
            + ")"
        )
        portfolio_sell(symbol, price, f"Stop-loss ({effective_stop} TRY)")
        _stoploss_cooldown[symbol] = datetime.now()
        _trailing_highs.pop(symbol, None)


def _dca_eligible(symbol: str, price: float, signal: "Signal") -> bool:
    """
    Returns True if conditions are met to add another tranche.

    Guards (all must pass):
      1. DCA is enabled globally
      2. Position exists for this symbol
      3. Tranche count is below DCA_MAX_TRANCHES
      4. Price has dropped at least DCA_MIN_DROP_PCT from the last tranche price
      5. Estimation points upward: trend is 'up' or 'sideways' (not 'down')
      6. RSI is above DCA_RSI_MIN (not in freefall)
    """
    if not DCA_ENABLED:
        return False

    pos = portfolio.positions.get(symbol)
    if not pos:
        return False

    if pos.tranche_count >= DCA_MAX_TRANCHES:
        log.debug(f"[DCA] {symbol}: max tranches ({DCA_MAX_TRANCHES}) reached.")
        return False

    drop_pct = (pos.last_tranche_price - price) / pos.last_tranche_price * 100
    if drop_pct < DCA_MIN_DROP_PCT:
        log.debug(
            f"[DCA] {symbol}: drop {drop_pct:.1f}% < required {DCA_MIN_DROP_PCT}% "
            f"(last tranche @ {pos.last_tranche_price})"
        )
        return False

    # Estimation must point upward
    trend = signal.auto_trend
    if trend == "down" and signal.trend_source != "default":
        log.debug(f"[DCA] {symbol}: blocked — trend is DOWN ({signal.trend_source}).")
        return False

    # RSI guard — don't average into freefall
    if signal.rsi is not None and signal.rsi < DCA_RSI_MIN:
        log.debug(f"[DCA] {symbol}: blocked — RSI={signal.rsi} < min {DCA_RSI_MIN}.")
        return False

    return True


def updateBalance(signal: "Signal", data: dict, stocks: dict):
    """
    Broker v3.0: Routes signals through multiple filters before acting.
      - Confidence gate
      - Time-of-day filter (quiet zone)
      - Cooldown after stop-loss
      - Risk/Reward ratio check
      - Dynamic position sizing
      - Partial profit taking at target 1
    """
    price  = data["price"]
    symbol = signal.symbol
    _current_prices[symbol] = price

    check_stop_loss(symbol, price, stocks)

    if signal.side == "BUY" and signal.strength in ("STRONG", "NORMAL", "BREAKOUT"):
        if symbol not in portfolio.positions:
            # ── Broker filters for NEW entries ──
            if signal.confidence < MIN_CONFIDENCE:
                log.debug(f"[GATE] {symbol}: confidence {signal.confidence}% < {MIN_CONFIDENCE}% — skipped")
                return
            if _in_quiet_zone():
                log.debug(f"[GATE] {symbol}: quiet zone — no new entries")
                return
            if _in_cooldown(symbol):
                log.debug(f"[GATE] {symbol}: cooldown active — no re-entry")
                return
            # R:R check
            s = stocks[symbol]
            stop = round(price * (1 - s["stop_pct"]), 2)
            target = s["resistance_1"]
            rr = _calc_risk_reward(price, stop, target)
            if rr < MIN_RISK_REWARD:
                log.debug(f"[GATE] {symbol}: R:R={rr} < {MIN_RISK_REWARD} — skipped")
                return

            portfolio_buy(symbol, price, signal.reason, stocks,
                          confidence=signal.confidence)
        else:
            # Existing position — attempt DCA
            if _dca_eligible(symbol, price, signal):
                pos      = portfolio.positions[symbol]
                drop_pct = (pos.last_tranche_price - price) / pos.last_tranche_price * 100
                dca_reason = (
                    f"DCA #{pos.tranche_count + 1} | "
                    f"price dropped {drop_pct:.1f}% from last tranche "
                    f"({pos.last_tranche_price} → {price} TRY) | "
                    f"trend={signal.auto_trend} RSI={signal.rsi}"
                )
                portfolio_add_tranche(symbol, price, dca_reason)

    elif signal.side == "SELL":
        s = stocks[symbol]
        # Partial profit at target 1 (resistance_2 zone)
        if price >= s["resistance_2"] and price < s["resistance_3"]:
            if symbol in portfolio.positions:
                pos = portfolio.positions[symbol]
                if pos.tranche_count > 0 and PARTIAL_PROFIT_PCT < 100:
                    portfolio_sell_partial(
                        symbol, price, PARTIAL_PROFIT_PCT,
                        f"Partial take-profit ({PARTIAL_PROFIT_PCT}%) at R2={s['resistance_2']} TRY"
                    )
                    return
        portfolio_sell(symbol, price, signal.reason)


# ─────────────────────────────────────────
#  DATA FETCHING — provider abstraction
# ─────────────────────────────────────────

_volume_history: dict = {}
VOLUME_WINDOW   = 20
_price_history:  dict = {}


def _update_price_history(symbol: str, price: float) -> list:
    if symbol not in _price_history:
        _price_history[symbol] = []
    h = _price_history[symbol]
    if not h or price != h[-1]:
        h.append(price)
    if len(h) > PRICE_WINDOW:
        h.pop(0)
    return h


def _parse_volume(volume_str: str) -> int:
    try:
        return int(float(str(volume_str).replace(".", "").replace(",", ".")))
    except (ValueError, TypeError):
        return 0


def _update_volume_avg(symbol: str, new_volume: int) -> tuple:
    if symbol not in _volume_history:
        _volume_history[symbol] = []
    history = _volume_history[symbol]
    if new_volume > 0 and (not history or new_volume != history[-1]):
        history.append(new_volume)
    if len(history) > VOLUME_WINDOW:
        history.pop(0)
    avg = int(sum(history) / len(history)) if history else new_volume or 1
    return avg, len(history)


# ── Bigpara provider ──────────────────────────────────────────────────────────

BIGPARA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://bigpara.hurriyet.com.tr/",
}


def _fetch_bigpara(symbol: str, retries: int = 3) -> Optional[dict]:
    """
    Fetches price data from Bigpara (~15 min delayed).
    Uses 'alis' (bid) during session and 'kapanis' (close) outside session.
    Retries up to 3 times on network errors (3s, 6s apart).
    Adds random jitter to reduce 401 rate-limiting.
    Known issue: under throttling, Bigpara returns the last cached value
    instead of a fresh price — the price history deduplication in
    _update_price_history() partially mitigates this.
    """
    import random
    time.sleep(random.uniform(0.3, 1.2))

    url = f"https://bigpara.hurriyet.com.tr/api/v1/borsa/hisseyuzeysel/{symbol}"
    for i in range(1, retries + 1):
        try:
            r = requests.get(url, headers=BIGPARA_HEADERS, timeout=10)
            r.raise_for_status()
            raw = r.json().get("data", {}).get("hisseYuzeysel", {})
            if not raw:
                log.warning(f"{symbol} [bigpara]: empty response.")
                return None

            bid   = raw.get("alis")
            close = raw.get("kapanis")
            raw_price = (bid if bid is not None else close) if session_open() \
                        else (close if close is not None else bid)

            if raw_price is None:
                log.warning(f"{symbol} [bigpara]: price field missing.")
                return None

            price  = float(str(raw_price).replace(",", "."))
            volume = _parse_volume(raw.get("hacimtl") or "0")
            change = float(str(raw.get("yuzdedegisim") or "0").replace(",", ".").replace("%", ""))
            return {"price": price, "volume": volume, "change": change}

        except requests.exceptions.RequestException as e:
            log.warning(f"{symbol} [bigpara] network error (attempt {i}/{retries}): {e}")
            if i < retries:
                time.sleep(3 * i)
            else:
                log.error(f"{symbol} [bigpara]: failed after {retries} attempts.")
                return None
        except (KeyError, ValueError, TypeError) as e:
            log.error(f"{symbol} [bigpara] parse error: {e}")
            return None


# ── Yahoo Finance provider ────────────────────────────────────────────────────

# Yahoo Finance chart API — same endpoint the browser uses, no API key required.
# Symbol convention: KCAER → KCAER.IS  (Istanbul Stock Exchange suffix)
# URL: https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}.IS?interval=1m&range=1d
#
# JSON structure used (from chart.result[0]):
#   meta.regularMarketPrice      → current price
#   meta.regularMarketVolume     → today's running total volume (share count)
#   meta.previousClose           → yesterday's close (for % change)
#   indicators.quote[0].close    → 1-min close prices for today (may contain nulls)
#   indicators.quote[0].volume   → 1-min volumes for today (may contain nulls)
#
# Key benefit over Bigpara: the 1-min close array pre-populates price_history
# with a full day of real bars → RSI, EMA, momentum are ready on the very first
# fetch instead of needing VOL_WARMUP_SCANS scans to warm up.

_YF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
_YF_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"


def _fetch_yfinance(symbol: str, retries: int = 3) -> Optional[dict]:
    """
    Fetches price + full-day 1-min history from Yahoo Finance chart API.
    Returns a dict with keys: price, volume, change, intraday_closes.
    intraday_closes is a list of non-null 1-min close prices for today —
    passed up to fetch_price() to pre-populate _price_history[symbol].
    """
    import random
    time.sleep(random.uniform(0.2, 0.8))

    url = f"{_YF_BASE}/{symbol}.IS"
    params = {"interval": "1m", "range": "1d"}

    for i in range(1, retries + 1):
        try:
            r = requests.get(url, headers=_YF_HEADERS, params=params, timeout=10)
            r.raise_for_status()
            body = r.json()

            result = body.get("chart", {}).get("result")
            if not result:
                err = body.get("chart", {}).get("error")
                log.warning(f"{symbol} [yfinance]: no result. error={err}")
                return None

            meta   = result[0].get("meta", {})
            quotes = result[0].get("indicators", {}).get("quote", [{}])[0]

            price = meta.get("regularMarketPrice")
            if price is None:
                log.warning(f"{symbol} [yfinance]: regularMarketPrice missing.")
                return None
            price = round(float(price), 4)

            volume     = int(meta.get("regularMarketVolume") or 0)
            prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
            change     = round((price - prev_close) / prev_close * 100, 2) \
                         if prev_close and prev_close > 0 else 0.0

            # Build intraday price history: filter nulls from 1-min close array
            raw_closes  = quotes.get("close") or []
            intraday    = [round(float(c), 4) for c in raw_closes if c is not None]

            log.debug(
                f"{symbol} [yfinance]: price={price} vol={volume:,} "
                f"change={change:+.2f}% intraday_bars={len(intraday)}"
            )
            return {
                "price":           price,
                "volume":          volume,
                "change":          change,
                "intraday_closes": intraday,   # pre-built price history
            }

        except requests.exceptions.RequestException as e:
            log.warning(f"{symbol} [yfinance] network error (attempt {i}/{retries}): {e}")
            if i < retries:
                time.sleep(3 * i)
            else:
                log.error(f"{symbol} [yfinance]: failed after {retries} attempts.")
                return None
        except (KeyError, ValueError, TypeError) as e:
            log.error(f"{symbol} [yfinance] parse error: {e}")
            return None


# ── Public fetch_price — routes to the configured provider ───────────────────

def fetch_price(symbol: str) -> Optional[dict]:
    """
    Routes to _fetch_bigpara() or _fetch_yfinance() based on PRICE_PROVIDER.
    Returns a unified dict consumed by generate_signal() and the rest of the bot:
      symbol, price, volume, avg_vol, vol_warmup, change, time, price_history

    yfinance extra behaviour:
      _fetch_yfinance() returns intraday_closes — a full day of 1-min close
      prices extracted from the Yahoo chart API response.  fetch_price() uses
      these to pre-populate _price_history[symbol] on every call, so RSI, EMA,
      and momentum are available from the very first scan without waiting for
      VOL_WARMUP_SCANS worth of scan cycles.
    """
    if PRICE_PROVIDER == "yfinance":
        raw = _fetch_yfinance(symbol)
    else:
        raw = _fetch_bigpara(symbol)

    if raw is None:
        return None

    price  = raw["price"]
    volume = raw["volume"]
    change = raw["change"]

    # yfinance: replace rolling price history with today's full 1-min bar set
    if PRICE_PROVIDER == "yfinance":
        intraday = raw.get("intraday_closes", [])
        if intraday:
            _price_history[symbol] = intraday[-PRICE_WINDOW:]
            log.debug(
                f"{symbol} [yfinance]: price_history pre-loaded "
                f"with {len(_price_history[symbol])} intraday bars"
            )

    avg_vol, warmup_count = _update_volume_avg(symbol, volume)
    price_hist = _update_price_history(symbol, price)
    _current_prices[symbol] = price

    return {
        "symbol":        symbol,
        "price":         price,
        "volume":        volume,
        "avg_vol":       avg_vol,
        "vol_warmup":    warmup_count,
        "change":        change,
        "time":          datetime.now().strftime("%H:%M"),
        "price_history": price_hist,
    }


# ─────────────────────────────────────────
#  ESTIMATION — INDICATORS
# ─────────────────────────────────────────

def compute_rsi(prices: list, period: int = RSI_PERIOD) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    deltas   = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
    recent   = deltas[-period:]
    avg_gain = sum(max(d, 0) for d in recent) / period
    avg_loss = sum(abs(min(d, 0)) for d in recent) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 1)


def compute_ema(prices: list, period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    k   = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 4)


def detect_trend(prices: list, manual_trend: Optional[str],
                  manual_strength: Optional[str]) -> tuple:
    """Returns (trend, strength, source)."""
    if manual_trend is not None:
        return manual_trend, manual_strength or "weak", "manual"
    ema_s = compute_ema(prices, EMA_SHORT)
    ema_l = compute_ema(prices, EMA_LONG)
    if ema_s is None or ema_l is None:
        return "sideways", "weak", "default"
    diff = (ema_s - ema_l) / ema_l * 100
    if diff > 3.0:    return "up",       "strong", "ema"
    if diff > 1.5:    return "up",       "weak",   "ema"
    if diff < -3.0:   return "down",     "strong", "ema"
    if diff < -1.5:   return "down",     "weak",   "ema"
    return "sideways", "weak", "ema"


def compute_momentum(prices: list, period: int = MOMENTUM_PERIOD) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    base = prices[-(period + 1)]
    return round((prices[-1] - base) / base * 100, 2) if base != 0 else None


def compute_confidence(side: str, rsi: Optional[float], volume_ok: bool,
                        trend: str, momentum: Optional[float]) -> int:
    score = 0
    if volume_ok:                                       score += 20
    if rsi is not None:
        if side == "BUY":
            if rsi < 30:      score += 20
            elif rsi <= 50:   score += 8
        elif side == "SELL":
            if rsi > 70:      score += 20
            elif rsi >= 50:   score += 8
    if side == "BUY":
        if trend == "up":         score += 20
        elif trend == "sideways": score += 8
    elif side == "SELL":
        if trend == "down":       score += 20
        elif trend == "sideways": score += 8
    if momentum is not None:
        if side == "BUY":
            if momentum < -0.5:        score += 20
            elif abs(momentum) <= 0.5: score += 8
        elif side == "SELL":
            if momentum > 0.5:         score += 20
            elif abs(momentum) <= 0.5: score += 8
    return min(score, 100)


def _confidence_label(score: int) -> str:
    if score >= 75: return "HIGH"
    if score >= 50: return "MODERATE"
    if score >= 25: return "LOW"
    return "VERY LOW"


# ─────────────────────────────────────────
#  SIGNAL ENGINE
# ─────────────────────────────────────────

@dataclass
class Signal:
    symbol:       str
    name:         str
    side:         str
    strength:     str
    price:        float
    reason:       str
    stop:         Optional[float]
    target_1:     Optional[float]
    target_2:     Optional[float]
    target_3:     Optional[float]
    volume_ok:    bool
    time:         str
    rsi:          Optional[float]
    momentum:     Optional[float]
    confidence:   int
    auto_trend:   str
    trend_source: str


def generate_signal(symbol: str, data: dict, stocks: dict) -> Signal:
    s          = stocks[symbol]
    price      = data["price"]
    volume     = data["volume"]
    avg_vol    = data["avg_vol"]
    warmup     = data.get("vol_warmup", VOL_WARMUP_SCANS)
    prices     = data.get("price_history", [])

    volume_ok  = (volume > avg_vol * s["volume_multiplier"]) and (warmup >= VOL_WARMUP_SCANS)
    vol_ratio  = (volume / avg_vol) if avg_vol > 0 else 0
    stop       = round(price * (1 - s["stop_pct"]), 2)

    rsi        = compute_rsi(prices)
    momentum   = compute_momentum(prices)
    trend, strength_t, trend_src = detect_trend(
        prices, s.get("trend"), s.get("trend_strength")
    )
    trend_info = f"trend={trend}/{strength_t}[{trend_src}]"

    log.debug(
        f"{symbol}: RSI={rsi} MOM={momentum}% {trend_info} "
        f"vol={vol_ratio:.1f}x warmup={warmup}/{VOL_WARMUP_SCANS} "
        f"prices={len(prices)}/{PRICE_WINDOW}"
    )

    def _make(side, strength, reason, sv, t1, t2, t3):
        conf = compute_confidence(side, rsi, volume_ok, trend, momentum)
        return Signal(symbol, s["name"], side, strength, price, reason,
                      sv, t1, t2, t3, volume_ok, data["time"],
                      rsi, momentum, conf, trend, trend_src)

    def _wait(reason: str) -> Signal:
        return Signal(symbol, s["name"], "WAIT", "NEUTRAL", price, reason,
                      None, None, None, None, volume_ok, data["time"],
                      rsi, momentum, 0, trend, trend_src)

    # ── BUY signals ──────────────────────────────────────────────────────

    if price <= s["strong_support"]:
        if trend == "down" and strength_t == "strong":
            return _wait(
                f"Strong support hit BUT strong downtrend — falling knife risk ⚠️ ({trend_info})"
            )
        rsi_note   = f" ⚠️ RSI={rsi} still elevated" if rsi is not None and rsi > 60 else ""
        vol_note   = " + high volume" if volume_ok else " ⚠️ low volume"
        trend_note = " ⚠️ weak downtrend — caution" if trend == "down" else ""
        strength   = "STRONG" if volume_ok else "STRONG (low vol)"
        return _make(
            "BUY", strength,
            f"Strong support ({s['strong_support']} TRY){vol_note}{trend_note}{rsi_note} | {trend_info}",
            stop, s["resistance_1"], s["resistance_2"], s["resistance_3"],
        )

    elif price <= s["mid_support"] and volume_ok:
        if trend == "down":
            return _wait(
                f"Mid support + volume BUT downtrend — waiting for reversal ({trend_info})"
            )
        mom_note = f" ⚠️ MOM={momentum:+.1f}% still dropping" \
                   if momentum is not None and momentum < -2.0 else ""
        return _make(
            "BUY", "NORMAL",
            f"Support zone ({s['mid_support']} TRY) + volume | {trend_info}{mom_note}",
            stop, s["resistance_1"], s["resistance_2"], None,
        )

    elif price > s["resistance_1"] and volume_ok:
        if trend == "down":
            return _wait(
                f"Breakout above R1 BUT downtrend — high false-breakout risk ({trend_info})"
            )
        rsi_note = f" ⚠️ RSI={rsi} overbought — breakout may be exhausted" \
                   if rsi is not None and rsi > 75 else ""
        return _make(
            "BUY", "BREAKOUT",
            f"Resistance broken ({s['resistance_1']} TRY) + volume ✅ | {trend_info}{rsi_note}",
            s["mid_support"], s["resistance_2"], s["resistance_3"], None,
        )

    # ── SELL signals ─────────────────────────────────────────────────────

    elif price >= s["resistance_3"]:
        return _make(
            "SELL", "TAKE PROFIT",
            f"3rd target ({s['resistance_3']} TRY) — close full position",
            None, None, None, None,
        )

    elif price >= s["resistance_2"]:
        if trend == "up" and strength_t == "strong" and (rsi is None or rsi < 75):
            return _wait(
                f"2nd target hit BUT strong uptrend (RSI={rsi}) — holding for 3rd target "
                f"({s['resistance_3']} TRY) ({trend_info})"
            )
        return _make(
            "SELL", "TAKE PROFIT",
            f"2nd target ({s['resistance_2']} TRY) — close 50% | {trend_info}",
            None, None, None, None,
        )

    # ── WAIT ─────────────────────────────────────────────────────────────
    else:
        rsi_str = f"RSI={rsi}" if rsi is not None else "RSI=warmup"
        mom_str = f"MOM={momentum:+.1f}%" if momentum is not None else "MOM=warmup"
        return _wait(
            f"Range-bound ({s['mid_support']}–{s['resistance_1']} TRY)"
            f" | vol {vol_ratio:.1f}x | {rsi_str} | {mom_str} | {trend_info}"
        )


# ─────────────────────────────────────────
#  NOTIFICATION — MESSAGE BUILDER
# ─────────────────────────────────────────

def _emoji(side: str, strength: str) -> str:
    if side == "BUY"  and strength == "STRONG":    return "🟢🔥"
    if side == "BUY"  and strength == "NORMAL":    return "🟢"
    if side == "BUY"  and strength == "BREAKOUT":  return "🚀"
    if side == "SELL":                             return "🔴"
    return "⏳"


def _confidence_bar(score: int) -> str:
    filled = round(score / 12.5)
    return "█" * filled + "░" * (8 - filled) + f" {score}%"


def _build_message(s: Signal, html: bool = False) -> str:
    emoji      = _emoji(s.side, s.strength)
    volume_str = "✅ High volume" if s.volume_ok else "⚠️ Low volume"
    rsi_str    = f"{s.rsi}" if s.rsi is not None else "warming up..."
    mom_str    = f"{s.momentum:+.2f}%" if s.momentum is not None else "warming up..."
    conf_label = _confidence_label(s.confidence)
    conf_bar   = _confidence_bar(s.confidence)
    divider    = "─" * 30

    trend_label = s.auto_trend.upper()
    if s.trend_source == "manual":    trend_label += " (manual)"
    elif s.trend_source == "default": trend_label += " (warmup)"

    # DCA status line for open positions
    dca_line = ""
    pos = portfolio.positions.get(s.symbol)
    if pos and DCA_ENABLED:
        remaining = DCA_MAX_TRANCHES - pos.tranche_count
        dca_line  = f"📦 DCA       : tranche {pos.tranche_count}/{DCA_MAX_TRANCHES} | {remaining} remaining"

    lines = [
        f"{emoji} {s.symbol} — {s.name}", divider,
        f"Price      : {s.price} TRY",
        f"Signal     : {s.side} ({s.strength})",
        f"Volume     : {volume_str}",
        f"Reason     : {s.reason}",
        divider,
        f"📊 RSI       : {rsi_str}",
        f"📈 Momentum  : {mom_str}",
        f"🔀 Trend     : {trend_label}",
        f"🎯 Confidence: {conf_bar} [{conf_label}]",
    ]
    if dca_line:
        lines.append(dca_line)
    lines.append(divider)

    if s.stop:     lines.append(f"Stop-Loss  : {s.stop} TRY")
    if s.target_1: lines.append(f"Target 1   : {s.target_1} TRY")
    if s.target_2: lines.append(f"Target 2   : {s.target_2} TRY")
    if s.target_3: lines.append(f"Target 3   : {s.target_3} TRY")
    lines += [divider, f"Time: {s.time}", "⚠️ This message is not investment advice."]

    if html:
        body = "".join(f"<p>{line}</p>" for line in lines)
        return f'<html><body style="font-family:monospace;font-size:14px;">{body}</body></html>'
    return f"{emoji} *{s.symbol} — {s.name}*\n" + "\n".join(lines[1:])


def _build_dca_message(symbol: str, pos: Position, price: float,
                        rsi: Optional[float], trend: str,
                        html: bool = False) -> str:
    """Dedicated alert message for a DCA tranche add."""
    divider   = "─" * 30
    drop_pct  = (pos.tranches[-2].price - price) / pos.tranches[-2].price * 100
    rsi_str   = f"{rsi}" if rsi is not None else "n/a"
    lines = [
        f"📉➕ DCA ADD — {symbol} — {pos.name}", divider,
        f"Tranche    : #{pos.tranche_count} of {DCA_MAX_TRANCHES}",
        f"Price      : {price} TRY  (↓{drop_pct:.1f}% from last tranche)",
        f"Avg cost   : {pos.avg_price:.4f} TRY",
        f"Total qty  : {pos.quantity} shares",
        divider,
        f"RSI        : {rsi_str}",
        f"Trend      : {trend.upper()}",
        divider,
        "Tranche breakdown:",
    ]
    for i, t in enumerate(pos.tranches, 1):
        lines.append(f"  #{i}: {t.quantity} shares @ {t.price} TRY  [{t.time}]")
    lines += [divider, "⚠️ This message is not investment advice."]

    if html:
        body = "".join(f"<p>{line}</p>" for line in lines)
        return f'<html><body style="font-family:monospace;font-size:14px;">{body}</body></html>'
    return "\n".join(lines)


# ─────────────────────────────────────────
#  NOTIFICATION — TELEGRAM & EMAIL
# ─────────────────────────────────────────

def send_telegram(text: str) -> bool:
    if not TELEGRAM_ENABLED or not TELEGRAM_TOKEN:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if r.status_code == 200:
            return True
        log.error(f"Telegram error {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log.error(f"Telegram connection error: {e}")
        return False


def send_email_msg(subject: str, plain: str, html_body: str) -> bool:
    if not EMAIL_ENABLED or not GMAIL_SENDER:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"BIST Signal Bot <{GMAIL_SENDER}>"
        msg["To"]      = GMAIL_RECIPIENT
        msg.attach(MIMEText(plain,     "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html",  "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_SENDER, GMAIL_PASSWORD)
            smtp.sendmail(GMAIL_SENDER, GMAIL_RECIPIENT, msg.as_string())
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("Gmail authentication failed! Use an App Password.")
        return False
    except Exception as e:
        log.error(f"Email could not be sent: {e}")
        return False


def send_alert(s: Signal):
    text = _build_message(s)
    if TELEGRAM_ENABLED:
        if send_telegram(text):
            log.info(f"Telegram sent: {s.symbol}")
    if EMAIL_ENABLED:
        subj = (
            f"[BIST] {_emoji(s.side, s.strength)} {s.symbol} — "
            f"{s.side} ({s.strength}) @ {s.price} TRY | "
            f"Confidence: {s.confidence}% [{_confidence_label(s.confidence)}]"
        )
        if send_email_msg(subj, _build_message(s, html=False), _build_message(s, html=True)):
            log.info(f"Email sent: {s.symbol}")


def send_dca_alert(symbol: str, pos: Position, price: float,
                    rsi: Optional[float], trend: str):
    """Sends a dedicated alert when a DCA tranche is added."""
    plain = _build_dca_message(symbol, pos, price, rsi, trend, html=False)
    hbody = _build_dca_message(symbol, pos, price, rsi, trend, html=True)
    subj  = (
        f"[BIST] 📉➕ DCA #{pos.tranche_count} — {symbol} "
        f"@ {price} TRY | avg={pos.avg_price:.4f} TRY"
    )
    if TELEGRAM_ENABLED:
        if send_telegram(plain):
            log.info(f"DCA Telegram sent: {symbol} tranche #{pos.tranche_count}")
    if EMAIL_ENABLED:
        if send_email_msg(subj, plain, hbody):
            log.info(f"DCA Email sent: {symbol} tranche #{pos.tranche_count}")


# ─────────────────────────────────────────
#  END-OF-DAY REPORT
# ─────────────────────────────────────────

def _build_eod_report(html: bool = False) -> str:
    divider      = "─" * 36
    today        = datetime.now().strftime("%d.%m.%Y")
    total        = portfolio.total_value(_current_prices)
    pnl          = portfolio.total_pnl(_current_prices)
    pnl_pct      = portfolio.pnl_pct(_current_prices)
    pnl_emoji    = "📈" if pnl >= 0 else "📉"
    pnl_str      = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
    buys         = [t for t in portfolio.trades if t.side == "BUY"]
    dcas         = [t for t in portfolio.trades if t.side == "DCA"]
    sells        = [t for t in portfolio.trades if t.side == "SELL"]
    total_comm   = sum(t.commission for t in portfolio.trades)
    realized_pnl = sum(t.pnl for t in sells if t.pnl is not None)

    lines = [
        f"📊 BIST SIMULATION REPORT — {today}", divider,
        f"💰 Starting  : {portfolio.starting_balance:>10.2f} TRY",
        f"💼 Total     : {total:>10.2f} TRY",
        f"{pnl_emoji} P&L      : {pnl_str:>10} TRY  ({pnl_pct:+.2f}%)",
        divider, f"🏦 Cash      : {portfolio.cash:>10.2f} TRY",
    ]

    if portfolio.positions:
        lines.append("📌 Open Positions:")
        for symbol, pos in portfolio.positions.items():
            current     = _current_prices.get(symbol, pos.avg_price)
            pos_pnl     = (current - pos.avg_price) * pos.quantity
            pos_pnl_str = f"+{pos_pnl:.2f}" if pos_pnl >= 0 else f"{pos_pnl:.2f}"
            hist        = _price_history.get(symbol, [])
            rsi_v       = compute_rsi(hist)
            mom_v       = compute_momentum(hist)
            rsi_s       = f"RSI={rsi_v}" if rsi_v is not None else "RSI=n/a"
            mom_s       = f"MOM={mom_v:+.1f}%" if mom_v is not None else "MOM=n/a"
            stop_price  = round(pos.avg_price * (1 - 0.04), 2)  # approximate display

            lines.append(
                f"  {symbol} [{pos.tranche_count} tranche(s)] | "
                f"qty={pos.quantity} | avg={pos.avg_price:.4f} TRY | "
                f"now={current} TRY | P&L: {pos_pnl_str} TRY | {rsi_s} {mom_s}"
            )
            # Show each tranche
            for i, t in enumerate(pos.tranches, 1):
                tranche_pnl = (current - t.price) * t.quantity
                tp_str = f"+{tranche_pnl:.2f}" if tranche_pnl >= 0 else f"{tranche_pnl:.2f}"
                lines.append(
                    f"    #{i}: {t.quantity} @ {t.price} TRY [{t.time}] | P&L: {tp_str} TRY"
                )
    else:
        lines.append("📌 Open Positions: None")

    lines += [
        divider, "📋 Trade Summary:",
        f"  Buys  : {len(buys)} trades",
        f"  DCAs  : {len(dcas)} trades",
        f"  Sells : {len(sells)} trades",
        f"  Realized P&L    : {realized_pnl:+.2f} TRY",
        f"  Total Commission: {total_comm:.2f} TRY",
    ]

    if portfolio.trades:
        lines.append(divider)
        lines.append("📝 Trade Details:")
        for t in portfolio.trades:
            pnl_s = f" | P&L: {t.pnl:+.2f} TRY" if t.pnl is not None else ""
            lines.append(
                f"  {t.time} | {t.side:4s} | {t.symbol} | "
                f"{t.quantity} shares @ {t.price} TRY{pnl_s}"
            )

    lines += [divider, "⚠️ Simulation data only. Not real trading."]

    if html:
        body = "".join(
            f"<p style='color:{'green' if '+' in line else ('red' if line.strip().startswith('-') else 'inherit')}'>{line}</p>"
            for line in lines
        )
        return f'<html><body style="font-family:monospace;font-size:13px;background:#f9f9f9;padding:20px;">{body}</body></html>'
    return "\n".join(lines)


def send_eod_email():
    if not EMAIL_ENABLED or not GMAIL_SENDER:
        return
    total = portfolio.total_value(_current_prices)
    pnl   = portfolio.total_pnl(_current_prices)
    subj  = (
        f"{'📈' if pnl >= 0 else '📉'} BIST Simulation Report — "
        f"{datetime.now().strftime('%d.%m.%Y')} | "
        f"Total: {total:.2f} TRY ({pnl:+.2f} TRY)"
    )
    if send_email_msg(subj, _build_eod_report(html=False), _build_eod_report(html=True)):
        log.info(f"[EOD] Report emailed → {GMAIL_RECIPIENT}")
    else:
        log.error("[EOD] Email failed.")


def send_eod_telegram():
    if not TELEGRAM_ENABLED or not TELEGRAM_TOKEN:
        return
    text = _build_eod_report(html=False)
    if len(text) > 4000:
        text = text[:4000] + "\n...(full report sent via email)"
    if send_telegram(text):
        log.info("[EOD] Report sent via Telegram.")


def end_of_day(stocks: dict):
    log.info("[EOD] Session closed, preparing report...")
    log.info("\n" + _build_eod_report())
    send_eod_email()
    send_eod_telegram()
    portfolio.day_start = datetime.now().strftime("%Y-%m-%d")
    portfolio.trades.clear()
    log.info("[EOD] Trade history cleared for next day.")


# ─────────────────────────────────────────
#  SESSION CHECK
# ─────────────────────────────────────────

def session_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    return SESSION_START <= now.hour < SESSION_END


last_signals:      dict = {}
_session_was_open: bool = False
_prev_stock_list:  set  = set()


def signal_changed(symbol: str, new: Signal) -> bool:
    old = last_signals.get(symbol)
    if old is None:
        return True
    return old.side != new.side or old.strength != new.strength


# ─────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────

def scan():
    global _session_was_open, _prev_stock_list

    stocks = scan_stocks()
    if not stocks:
        log.warning("No stocks loaded! Check the stocks/ folder.")
        return

    current_list = set(stocks.keys())
    added        = current_list - _prev_stock_list
    removed      = _prev_stock_list - current_list
    if added:
        log.info(f"✅ New stock(s) added: {', '.join(sorted(added))}")
    if removed:
        log.info(f"🗑️  Stock(s) removed: {', '.join(sorted(removed))}")
        for sym in removed:
            last_signals.pop(sym, None)
            _price_history.pop(sym, None)
    _prev_stock_list = current_list

    is_open = session_open()
    if _session_was_open and not is_open:
        end_of_day(stocks)
    _session_was_open = is_open

    if not is_open:
        log.info("Session closed, waiting...")
        return

    log.info(f"Scan started... ({len(stocks)} stocks: {', '.join(sorted(stocks))})")

    for symbol in sorted(stocks):
        data = fetch_price(symbol)
        if not data:
            log.warning(f"{symbol}: Data unavailable, skipping.")
            continue

        signal    = generate_signal(symbol, data, stocks)
        vol_ratio = (data["volume"] / data["avg_vol"]) if data["avg_vol"] > 0 else 0
        warmup    = data.get("vol_warmup", 0)
        warmup_s  = "" if warmup >= VOL_WARMUP_SCANS else f" ⏳vol-warmup {warmup}/{VOL_WARMUP_SCANS}"

        prev_price = last_signals[symbol].price if symbol in last_signals else data["price"]
        price_chg  = data["price"] - prev_price
        chg_str    = f" ({price_chg:+.2f})" if price_chg != 0 else ""

        s        = stocks[symbol]
        dist_sup = round(data["price"] - s["strong_support"], 2)
        dist_res = round(s["resistance_1"] - data["price"], 2)

        rsi_str  = f"RSI={signal.rsi}" if signal.rsi is not None else "RSI=⏳"
        mom_str  = f"MOM={signal.momentum:+.1f}%" if signal.momentum is not None else "MOM=⏳"
        conf_str = (f"conf={signal.confidence}%[{_confidence_label(signal.confidence)}]"
                    if signal.side != "WAIT" else "")

        # DCA position summary for log
        pos = portfolio.positions.get(symbol)
        dca_str = (f" | DCA:{pos.tranche_count}/{DCA_MAX_TRANCHES} avg={pos.avg_price:.2f}"
                   if pos else "")

        log.info(
            f"{symbol}: {signal.price} TRY{chg_str} → {signal.side} ({signal.strength})"
            f" | vol {vol_ratio:.1f}x{warmup_s}"
            f" | sup+{dist_sup:.2f} res-{dist_res:.2f}"
            f" | {rsi_str} {mom_str} trend={signal.auto_trend}[{signal.trend_source}]"
            + (f" | {conf_str}" if conf_str else "")
            + dca_str
        )

        check_stop_loss(symbol, signal.price, stocks)

        if signal.side == "BUY" and signal.strength in ("STRONG", "NORMAL", "BREAKOUT"):
            if symbol not in portfolio.positions:
                # Normal first buy — only alert on signal change
                if signal_changed(symbol, signal):
                    send_alert(signal)
                updateBalance(signal, data, stocks)
            else:
                # Position exists — attempt DCA, send dedicated DCA alert if added
                pos_before = portfolio.positions[symbol].tranche_count
                updateBalance(signal, data, stocks)
                pos_after  = portfolio.positions.get(symbol)
                if pos_after and pos_after.tranche_count > pos_before:
                    send_dca_alert(symbol, pos_after, data["price"],
                                   signal.rsi, signal.auto_trend)

        elif signal.side == "SELL":
            if signal_changed(symbol, signal):
                send_alert(signal)
            updateBalance(signal, data, stocks)

        last_signals[symbol] = signal

    log.info("Scan complete.")


# ─────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────

if __name__ == "__main__":
    create_sample_files()
    stocks = scan_stocks()

    log.info("=" * 57)
    log.info("  BIST Signal Bot v3.0 — Broker Edition")
    log.info(f"  Loaded stocks    : {', '.join(sorted(stocks)) if stocks else 'NONE'}")
    log.info(f"  Stocks folder    : {os.path.abspath(STOCKS_FOLDER)}/")
    log.info(f"  Log file         : {os.path.abspath(LOG_FILE)}")
    log.info(f"  Scan interval    : {SCAN_INTERVAL} min")
    log.info(f"  Price provider   : {PRICE_PROVIDER.upper()}"
             + (" ⚠️  volume in shares, not TRY — recalibrate volume_multiplier" if PRICE_PROVIDER == "yfinance" else ""))
    log.info(f"  Vol warmup       : {VOL_WARMUP_SCANS} unique readings required")
    log.info(f"  Price window     : {PRICE_WINDOW} readings max")
    log.info(f"  RSI period       : {RSI_PERIOD} scans")
    log.info(f"  Momentum period  : {MOMENTUM_PERIOD} scans (~{MOMENTUM_PERIOD * SCAN_INTERVAL} min)")
    log.info(f"  EMA short/long   : EMA{EMA_SHORT} / EMA{EMA_LONG}")
    log.info(f"  DCA              : {'ON' if DCA_ENABLED else 'OFF'}")
    if DCA_ENABLED:
        log.info(f"  DCA max tranches : {DCA_MAX_TRANCHES} (including first buy)")
        log.info(f"  DCA min drop     : {DCA_MIN_DROP_PCT}% from last tranche price")
        log.info(f"  DCA RSI min      : RSI > {DCA_RSI_MIN} required (not in freefall)")
    log.info(f"  ── Broker v3.0 ──")
    log.info(f"  Trailing stop    : {'ON' if TRAILING_STOP_ENABLED else 'OFF'} ({TRAILING_STOP_PCT}% from high)")
    log.info(f"  Breakeven stop   : after +{BREAKEVEN_TRIGGER_PCT}% gain")
    log.info(f"  Partial profit   : sell {PARTIAL_PROFIT_PCT}% at target 1")
    log.info(f"  Min confidence   : {MIN_CONFIDENCE}%")
    log.info(f"  Quiet zone       : skip first {AVOID_OPEN_MINUTES}m / last {AVOID_CLOSE_MINUTES}m")
    log.info(f"  Dynamic sizing   : {'ON' if DYNAMIC_SIZING else 'OFF'} (15%-40% by confidence)")
    log.info(f"  Min R:R ratio    : {MIN_RISK_REWARD}:1")
    log.info(f"  Stop cooldown    : {STOPLOSS_COOLDOWN_MIN} min")
    log.info(f"  Starting balance : {portfolio.starting_balance:,.0f} TRY")
    log.info(f"  Telegram : {'ON' if TELEGRAM_ENABLED else 'OFF'}")
    log.info(f"  Email    : {'ON' if EMAIL_ENABLED else 'OFF'}")
    log.info("=" * 57)
    log.info("💡 To add a stock: create stocks/SYMBOL.txt")
    log.info("💡 DCA adds tranches when price drops ≥ DCA_MIN_DROP_PCT and trend is up/sideways")
    log.info("💡 Switch provider: set PRICE_PROVIDER=yfinance in .env (pip install yfinance first)")

    now = datetime.now()
    if not session_open():
        if now.weekday() < 5 and now.hour < SESSION_START:
            wake = now.replace(hour=SESSION_START, minute=0, second=5, microsecond=0)
            secs = (wake - now).seconds
            log.info(f"Market opens at {SESSION_START}:00. Sleeping {secs//60}m{secs%60}s ...")
            time.sleep(secs)
        else:
            log.info("Session closed. Bot will wait for next session.")

    scan()
    schedule.every(SCAN_INTERVAL).minutes.do(scan)

    log.info(f"Bot running. Scanning every {SCAN_INTERVAL} min. Press Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)
