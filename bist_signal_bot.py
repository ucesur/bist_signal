"""
BIST Stock Signal Bot
======================
Stocks     : Auto-loaded from .txt files in the stocks/ folder
Interval   : 10 minutes (schedule)
Alerts     : Telegram + Gmail (smtplib)
Data       : Bigpara (~15 min delayed, no registration required)
Simulation : 10,000 TRY starting balance, end-of-day email report

Estimation (v2.0 additions):
  - RSI          : 14-scan RSI, oversold/overbought zones
  - Momentum     : 5-scan rate-of-change % (falling knife / breakout confirmation)
  - Auto trend   : EMA5 vs EMA20 crossover replaces manual trend/trend_strength fields
  - Confidence   : 0-100 score combining volume, RSI, trend, momentum

New .env parameters:
  RSI_PERIOD      = 14   (lower = faster but noisier signals)
  MOMENTUM_PERIOD = 5    (scans for rate-of-change, 5 × 10 min = ~50 min)
  EMA_SHORT       = 5    (fast EMA for trend detection)
  EMA_LONG        = 20   (slow EMA for trend detection)
  PRICE_WINDOW    = 50   (max price history readings per symbol)

To add a new stock:
  Create a stocks/SYMBOL.txt file — the bot picks it up on the next scan.
  The trend / trend_strength fields in .txt are now optional manual overrides.
  If omitted, trend is auto-detected via EMA crossover once enough data is collected.

Setup:
  pip install requests schedule python-dotenv
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

LOG_FILE     = os.getenv("LOG_FILE", "bist.log")
LOG_MAX_MB   = int(os.getenv("LOG_MAX_MB", "10"))
LOG_BACKUPS  = int(os.getenv("LOG_BACKUPS", "7"))
LOG_LEVEL    = os.getenv("LOG_LEVEL", "INFO").upper()

_fmt     = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
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
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GMAIL_SENDER     = os.getenv("GMAIL_GONDEREN", "")
GMAIL_PASSWORD   = os.getenv("GMAIL_SIFRE", "")
GMAIL_RECIPIENT  = os.getenv("GMAIL_ALICI", "")
TELEGRAM_ENABLED = os.getenv("TELEGRAM_AKTIF", "true").lower() == "true"
EMAIL_ENABLED    = os.getenv("EMAIL_AKTIF", "true").lower() == "true"

STOCKS_FOLDER    = os.getenv("HISSELER_KLASOR", "stocks")

SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL_MIN", "10"))
VOL_WARMUP_SCANS = int(os.getenv("VOL_WARMUP_SCANS",  "5"))
SESSION_START    = 10
SESSION_END      = 18
COMMISSION_RATE  = 0.001

# ── Estimation parameters ──────────────────
RSI_PERIOD       = int(os.getenv("RSI_PERIOD",       "14"))
MOMENTUM_PERIOD  = int(os.getenv("MOMENTUM_PERIOD",  "5"))
EMA_SHORT        = int(os.getenv("EMA_SHORT",        "5"))
EMA_LONG         = int(os.getenv("EMA_LONG",         "20"))
PRICE_WINDOW     = int(os.getenv("PRICE_WINDOW",     "50"))

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
    Returns: (symbol, data_dict) or None on error.

    File format (SYMBOL.txt):
        name              = Kocaer Steel
        strong_support    = 11.00
        mid_support       = 11.80
        resistance_1      = 12.20
        resistance_2      = 12.60
        resistance_3      = 14.05
        stop_pct          = 0.04
        volume_multiplier = 1.5
        # trend / trend_strength are now optional — auto-detected via EMA crossover.
        # You can still set them manually here as a permanent override:
        # trend             = up
        # trend_strength    = strong
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
                        value = float(value.replace(",", "."))
                    except ValueError:
                        log.warning(f"{file_path}:{line_no} — '{key}' could not be converted to float: {value!r}")
                        return None
                    data[key] = value

        missing = [f for f in REQUIRED_FIELDS if f not in data]
        if missing:
            log.error(f"{file_path} — Missing fields: {missing}")
            return None

        # trend / trend_strength are optional — None means "use auto-detection"
        # If manually set in the file, they act as a permanent override.
        data.setdefault("trend",          None)   # None → auto
        data.setdefault("trend_strength", None)   # None → auto

        return symbol, data

    except FileNotFoundError:
        log.error(f"{file_path} — File not found.")
        return None
    except Exception as e:
        log.error(f"{file_path} — Read error: {e}")
        return None


def scan_stocks() -> dict:
    """
    Scans all .txt files in the stocks/ folder and returns the stocks dictionary.
    Automatically picks up new files and removes deleted ones.
    """
    if not os.path.isdir(STOCKS_FOLDER):
        os.makedirs(STOCKS_FOLDER)
        log.info(f"'{STOCKS_FOLDER}/' folder created.")

    files  = glob.glob(os.path.join(STOCKS_FOLDER, "*.txt"))
    stocks = {}
    for file in sorted(files):
        result = load_stock(file)
        if result:
            symbol, data = result
            stocks[symbol] = data

    return stocks


# ─────────────────────────────────────────
#  CREATE SAMPLE TXT FILES
# ─────────────────────────────────────────

SAMPLE_STOCKS = {
    "KCAER.txt": """\
# Kocaer Steel — Technical Levels
# Updated: 18.03.2026
# Lines starting with # are comments.
# trend / trend_strength are optional — auto-detected by EMA crossover.

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
# Updated: 18.03.2026

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
# Updated: 18.03.2026

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
    """Creates sample stock files if the stocks folder is empty."""
    if not os.path.isdir(STOCKS_FOLDER):
        os.makedirs(STOCKS_FOLDER)
    existing = glob.glob(os.path.join(STOCKS_FOLDER, "*.txt"))
    if existing:
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
class Position:
    symbol:      str
    name:        str
    quantity:    int
    buy_price:   float
    buy_time:    str

@dataclass
class Trade:
    time:       str
    symbol:     str
    side:       str
    price:      float
    quantity:   int
    amount:     float
    commission: float
    pnl:        Optional[float]
    reason:     str

@dataclass
class Portfolio:
    starting_balance:      float = 10_000.0
    cash:                  float = 10_000.0
    positions:             dict  = field(default_factory=dict)
    trades:                list  = field(default_factory=list)
    day_start:             str   = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    POSITION_SIZE_PCT:     float = 0.30

    def total_value(self, current_prices: dict) -> float:
        stock_value = sum(
            p.quantity * current_prices.get(p.symbol, p.buy_price)
            for p in self.positions.values()
        )
        return self.cash + stock_value

    def total_pnl(self, current_prices: dict) -> float:
        return self.total_value(current_prices) - self.starting_balance

    def pnl_pct(self, current_prices: dict) -> float:
        return (self.total_pnl(current_prices) / self.starting_balance) * 100


portfolio        = Portfolio()
_current_prices: dict = {}


def portfolio_buy(symbol: str, price: float, reason: str, stocks: dict) -> bool:
    if symbol in portfolio.positions:
        log.info(f"[SIM] {symbol}: Position already open, buy skipped.")
        return False
    allocate = portfolio.cash * portfolio.POSITION_SIZE_PCT
    if allocate < price:
        log.info(f"[SIM] {symbol}: Insufficient cash ({portfolio.cash:.2f} TRY).")
        return False
    quantity   = int(allocate / price)
    if quantity == 0:
        return False
    amount     = quantity * price
    commission = amount * COMMISSION_RATE
    portfolio.cash -= (amount + commission)
    portfolio.positions[symbol] = Position(
        symbol=symbol, name=stocks[symbol]["name"],
        quantity=quantity, buy_price=price,
        buy_time=datetime.now().strftime("%H:%M"),
    )
    portfolio.trades.append(Trade(
        time=datetime.now().strftime("%H:%M"), symbol=symbol,
        side="BUY", price=price, quantity=quantity, amount=amount,
        commission=commission, pnl=None, reason=reason,
    ))
    log.info(f"[SIM] BUY | {symbol} | {quantity} shares @ {price} TRY | Cash: {portfolio.cash:.2f} TRY")
    return True


def portfolio_sell(symbol: str, price: float, reason: str) -> bool:
    if symbol not in portfolio.positions:
        return False
    pos        = portfolio.positions[symbol]
    amount     = pos.quantity * price
    commission = amount * COMMISSION_RATE
    net        = amount - commission
    pnl        = net - pos.quantity * pos.buy_price - pos.quantity * pos.buy_price * COMMISSION_RATE
    portfolio.cash += net
    del portfolio.positions[symbol]
    portfolio.trades.append(Trade(
        time=datetime.now().strftime("%H:%M"), symbol=symbol,
        side="SELL", price=price, quantity=pos.quantity, amount=amount,
        commission=commission, pnl=pnl, reason=reason,
    ))
    pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
    log.info(f"[SIM] SELL | {symbol} | {pos.quantity} shares @ {price} TRY | P&L: {pnl_str} TRY")
    return True


def check_stop_loss(symbol: str, price: float, stocks: dict):
    if symbol not in portfolio.positions:
        return
    pos        = portfolio.positions[symbol]
    stop_price = round(pos.buy_price * (1 - stocks[symbol]["stop_pct"]), 2)
    if price <= stop_price:
        log.warning(f"[SIM] STOP-LOSS triggered! {symbol} @ {price} TRY (stop: {stop_price} TRY)")
        portfolio_sell(symbol, price, f"Stop-loss ({stop_price} TRY)")


def updateBalance(signal: "Signal", data: dict, stocks: dict):
    price  = data["price"]
    symbol = signal.symbol
    _current_prices[symbol] = price
    check_stop_loss(symbol, price, stocks)
    if signal.side == "BUY" and signal.strength in ("STRONG", "NORMAL", "BREAKOUT"):
        portfolio_buy(symbol, price, signal.reason, stocks)
    elif signal.side == "SELL":
        portfolio_sell(symbol, price, signal.reason)


# ─────────────────────────────────────────
#  DATA FETCHING — Bigpara API
# ─────────────────────────────────────────

_volume_history: dict = {}
VOLUME_WINDOW   = 20

# ── Price history buffer (NEW) ─────────────
_price_history: dict = {}

BIGPARA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://bigpara.hurriyet.com.tr/",
}


def _update_price_history(symbol: str, price: float) -> list:
    """
    Appends price to the rolling history buffer. Skips exact duplicates
    (same reason as volume: Bigpara sometimes caches the last value on 401).
    Returns the current history list.
    """
    if symbol not in _price_history:
        _price_history[symbol] = []
    h = _price_history[symbol]
    if not h or price != h[-1]:
        h.append(price)
    if len(h) > PRICE_WINDOW:
        h.pop(0)
    return h


def fetch_price(symbol: str, retries: int = 3) -> Optional[dict]:
    """
    Fetches stock data from Bigpara (~15 min delayed).
    Retries up to 3 times on connection errors (3s, 6s apart).
    Uses 'alis' (bid) during session and 'kapanis' (close) outside session.
    Adds a small random jitter before each request to reduce 401 rate-limiting.
    """
    import random
    time.sleep(random.uniform(0.3, 1.2))

    url = f"https://bigpara.hurriyet.com.tr/api/v1/borsa/hisseyuzeysel/{symbol}"
    for i in range(1, retries + 1):
        try:
            r = requests.get(url, headers=BIGPARA_HEADERS, timeout=10)
            r.raise_for_status()
            data = r.json().get("data", {}).get("hisseYuzeysel", {})
            if not data:
                log.warning(f"{symbol}: Bigpara returned empty data.")
                return None

            bid   = data.get("alis")
            close = data.get("kapanis")

            if session_open():
                raw_price = bid if bid is not None else close
            else:
                raw_price = close if close is not None else bid

            if raw_price is None:
                log.warning(f"{symbol}: Price field is empty.")
                return None

            price    = float(str(raw_price).replace(",", "."))
            volume   = _parse_volume(data.get("hacimtl") or "0")
            change   = float(str(data.get("yuzdedegisim") or "0").replace(",", ".").replace("%", ""))
            avg_vol, warmup_count = _update_volume_avg(symbol, volume)

            # ── Collect price history ──────────
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
                "price_history": price_hist,     # NEW — passed into signal engine
            }

        except requests.exceptions.RequestException as e:
            log.warning(f"{symbol} network error (attempt {i}/{retries}): {e}")
            if i < retries:
                time.sleep(3 * i)
            else:
                log.error(f"{symbol}: Failed after {retries} attempts.")
                return None
        except (KeyError, ValueError, TypeError) as e:
            log.error(f"{symbol} data error: {e}")
            return None


def _parse_volume(volume_str: str) -> int:
    try:
        return int(float(str(volume_str).replace(".", "").replace(",", ".")))
    except (ValueError, TypeError):
        return 0


def _update_volume_avg(symbol: str, new_volume: int) -> tuple:
    """Returns (avg_vol, warmup_count)."""
    if symbol not in _volume_history:
        _volume_history[symbol] = []
    history = _volume_history[symbol]
    if new_volume > 0 and (not history or new_volume != history[-1]):
        history.append(new_volume)
    if len(history) > VOLUME_WINDOW:
        history.pop(0)
    avg = int(sum(history) / len(history)) if history else new_volume or 1
    return avg, len(history)


# ─────────────────────────────────────────
#  ESTIMATION — INDICATORS
# ─────────────────────────────────────────

def compute_rsi(prices: list, period: int = RSI_PERIOD) -> Optional[float]:
    """
    Classic RSI using simple average gains/losses over the last `period` price changes.
    Returns None if not enough data (needs period+1 readings minimum).

    Interpretation:
      RSI < 30  → oversold   → supports BUY signals
      RSI > 70  → overbought → supports SELL signals
      30-70     → neutral zone
    """
    if len(prices) < period + 1:
        return None
    deltas   = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
    recent   = deltas[-period:]
    gains    = [max(d, 0) for d in recent]
    losses   = [abs(min(d, 0)) for d in recent]
    avg_gain = sum(gains)  / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def compute_ema(prices: list, period: int) -> Optional[float]:
    """
    Exponential Moving Average over the given period.
    Returns None if not enough data.
    """
    if len(prices) < period:
        return None
    k   = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 4)


def detect_trend(prices: list, manual_trend: Optional[str],
                  manual_strength: Optional[str]) -> tuple:
    """
    Returns (trend: str, strength: str, source: str).

    Priority:
      1. If manual_trend is set in the .txt file → use it (permanent override).
      2. Otherwise → compute from EMA_SHORT vs EMA_LONG crossover.
      3. If not enough price history yet → fall back to 'sideways/weak'.

    trend    : 'up' | 'down' | 'sideways'
    strength : 'strong' | 'weak'
    source   : 'manual' | 'ema' | 'default' (for logging)

    EMA sensitivity thresholds (tunable via EMA_SHORT / EMA_LONG):
      diff_pct > +1.5%  → up/weak    | diff_pct > +3.0% → up/strong
      diff_pct < -1.5%  → down/weak  | diff_pct < -3.0% → down/strong
      else              → sideways/weak
    """
    if manual_trend is not None:
        return manual_trend, manual_strength or "weak", "manual"

    ema_s = compute_ema(prices, EMA_SHORT)
    ema_l = compute_ema(prices, EMA_LONG)

    if ema_s is None or ema_l is None:
        return "sideways", "weak", "default"

    diff_pct = (ema_s - ema_l) / ema_l * 100

    if diff_pct > 3.0:
        return "up",       "strong", "ema"
    elif diff_pct > 1.5:
        return "up",       "weak",   "ema"
    elif diff_pct < -3.0:
        return "down",     "strong", "ema"
    elif diff_pct < -1.5:
        return "down",     "weak",   "ema"
    else:
        return "sideways", "weak",   "ema"


def compute_momentum(prices: list, period: int = MOMENTUM_PERIOD) -> Optional[float]:
    """
    Rate of Change (ROC) over `period` scans, expressed as a percentage.
    Returns None if not enough data.

    Interpretation:
      Negative near support → falling knife risk (weakens BUY confidence)
      Positive near resistance → breakout confirmation (raises BUY confidence)
    """
    if len(prices) < period + 1:
        return None
    base = prices[-(period + 1)]
    if base == 0:
        return None
    return round((prices[-1] - base) / base * 100, 2)


def compute_confidence(side: str, rsi: Optional[float], volume_ok: bool,
                        trend: str, momentum: Optional[float]) -> int:
    """
    Aggregates four independent signals into a 0-100 confidence score.

    Each component contributes up to 25 points:
      Volume    : +25 if volume above threshold (volume_ok)
      RSI       : +25 if RSI is in the zone that agrees with signal direction
                  +10 if RSI is neutral (30-70 for BUY, 30-70 for SELL)
      Trend     : +25 if trend aligns with signal direction
                  +10 if sideways (not opposed)
      Momentum  : +25 if momentum aligns (negative = dip for BUY, positive = push for SELL)
                  +10 if momentum is near zero (< 0.5% absolute)

    Score guide:
      75-100 : High confidence — all factors agree
      50-74  : Moderate — most factors agree
      25-49  : Low — mixed signals, trade with caution
      0-24   : Very low — most factors oppose the signal
    """
    score = 0

    # Volume
    if volume_ok:
        score += 25

    # RSI
    if rsi is not None:
        if side == "BUY":
            if rsi < 30:        score += 25   # oversold — ideal buy zone
            elif rsi <= 50:     score += 10   # cooling but not bottomed
        elif side == "SELL":
            if rsi > 70:        score += 25   # overbought — ideal sell zone
            elif rsi >= 50:     score += 10   # elevated but not peak

    # Trend
    if side == "BUY":
        if trend == "up":       score += 25
        elif trend == "sideways": score += 10
        # down trend contributes 0 (already penalised by signal engine filters)
    elif side == "SELL":
        if trend == "down":     score += 25
        elif trend == "sideways": score += 10

    # Momentum
    if momentum is not None:
        if side == "BUY":
            if momentum < -0.5:   score += 25  # price dipped — buying the dip
            elif abs(momentum) <= 0.5: score += 10  # stable
        elif side == "SELL":
            if momentum > 0.5:    score += 25  # price still rising — good exit
            elif abs(momentum) <= 0.5: score += 10  # stable

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
    side:         str            # BUY / SELL / WAIT
    strength:     str            # STRONG / NORMAL / BREAKOUT / TAKE PROFIT / NEUTRAL
    price:        float
    reason:       str
    stop:         Optional[float]
    target_1:     Optional[float]
    target_2:     Optional[float]
    target_3:     Optional[float]
    volume_ok:    bool
    time:         str
    # ── Estimation fields (NEW) ──────────
    rsi:          Optional[float]   # RSI value, None during warmup
    momentum:     Optional[float]   # Rate of change %, None during warmup
    confidence:   int               # 0-100 confidence score
    auto_trend:   str               # 'up' / 'down' / 'sideways' (resolved trend)
    trend_source: str               # 'manual' / 'ema' / 'default'


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

    # ── Estimation indicators ────────────
    rsi        = compute_rsi(prices)
    momentum   = compute_momentum(prices)
    trend, strength_t, trend_src = detect_trend(
        prices,
        s.get("trend"),          # None if not set in .txt
        s.get("trend_strength"), # None if not set in .txt
    )
    trend_info = f"trend={trend}/{strength_t}[{trend_src}]"

    log.debug(
        f"{symbol}: RSI={rsi} MOM={momentum}% {trend_info} "
        f"vol={vol_ratio:.1f}x warmup={warmup}/{VOL_WARMUP_SCANS} "
        f"prices_collected={len(prices)}/{PRICE_WINDOW}"
    )

    dist_sup = round(price - s["strong_support"], 2)
    dist_res = round(s["resistance_1"] - price, 2)
    log.debug(f"{symbol}: +{dist_sup:.2f} above support | {dist_res:.2f} below resistance_1")

    def _make(side, strength, reason, stop_v, t1, t2, t3):
        conf = compute_confidence(side, rsi, volume_ok, trend, momentum) if side != "WAIT" else 0
        return Signal(
            symbol, s["name"], side, strength, price, reason,
            stop_v, t1, t2, t3, volume_ok, data["time"],
            rsi, momentum, conf, trend, trend_src,
        )

    def _wait(reason: str) -> Signal:
        return Signal(
            symbol, s["name"], "WAIT", "NEUTRAL", price, reason,
            None, None, None, None, volume_ok, data["time"],
            rsi, momentum, 0, trend, trend_src,
        )

    # ── BUY signals ──────────────────────────────────────────────────────

    if price <= s["strong_support"]:
        # Falling knife guard: strong downtrend → skip entirely
        if trend == "down" and strength_t == "strong":
            return _wait(
                f"Strong support hit BUT strong downtrend — falling knife risk ⚠️ ({trend_info})"
            )
        # RSI still very high despite hitting support → suspicious, caution note
        rsi_note = ""
        if rsi is not None and rsi > 60:
            rsi_note = f" ⚠️ RSI={rsi} still elevated — caution"

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
        # Extra caution: momentum still falling fast
        mom_note = ""
        if momentum is not None and momentum < -2.0:
            mom_note = f" ⚠️ momentum {momentum:+.1f}% — still dropping"
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
        # RSI overbought on breakout → likely exhaustion, not momentum
        rsi_note = ""
        if rsi is not None and rsi > 75:
            rsi_note = f" ⚠️ RSI={rsi} overbought — breakout may be exhausted"
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
        # Strong uptrend + RSI not yet overbought → hold for 3rd target
        if trend == "up" and strength_t == "strong" and (rsi is None or rsi < 75):
            return _wait(
                f"2nd target hit BUT strong uptrend (RSI={rsi}) — holding for 3rd target "
                f"({s['resistance_3']} TRY) ({trend_info})"
            )
        return _make(
            "SELL", "TAKE PROFIT",
            f"2nd target ({s['resistance_2']} TRY) — close 50% of position | {trend_info}",
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
    """Visual bar for Telegram — e.g. ████░░░░ 75%"""
    filled = round(score / 12.5)   # 8 segments total
    return "█" * filled + "░" * (8 - filled) + f" {score}%"


def _build_message(s: Signal, html: bool = False) -> str:
    emoji      = _emoji(s.side, s.strength)
    volume_str = "✅ High volume" if s.volume_ok else "⚠️ Low volume"
    rsi_str    = f"{s.rsi}" if s.rsi is not None else "warming up..."
    mom_str    = (f"{s.momentum:+.2f}%" if s.momentum is not None else "warming up...")
    conf_label = _confidence_label(s.confidence)
    conf_bar   = _confidence_bar(s.confidence)
    divider    = "─" * 30

    trend_label = s.auto_trend.upper()
    if s.trend_source == "manual":
        trend_label += " (manual)"
    elif s.trend_source == "default":
        trend_label += " (warmup)"

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
        divider,
    ]
    if s.stop:     lines.append(f"Stop-Loss  : {s.stop} TRY")
    if s.target_1: lines.append(f"Target 1   : {s.target_1} TRY")
    if s.target_2: lines.append(f"Target 2   : {s.target_2} TRY")
    if s.target_3: lines.append(f"Target 3   : {s.target_3} TRY")
    lines += [divider, f"Time: {s.time}", "⚠️ This message is not investment advice."]

    if html:
        body = "".join(f"<p>{line}</p>" for line in lines)
        return f'<html><body style="font-family:monospace;font-size:14px;">{body}</body></html>'
    return f"{emoji} *{s.symbol} — {s.name}*\n" + "\n".join(lines[1:])


# ─────────────────────────────────────────
#  NOTIFICATION — TELEGRAM
# ─────────────────────────────────────────

def send_telegram(s: Signal) -> bool:
    if not TELEGRAM_ENABLED or not TELEGRAM_TOKEN:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": _build_message(s), "parse_mode": "Markdown"},
            timeout=10,
        )
        if r.status_code == 200:
            log.info(f"Telegram sent: {s.symbol}")
            return True
        log.error(f"Telegram error {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log.error(f"Telegram connection error: {e}")
        return False


# ─────────────────────────────────────────
#  NOTIFICATION — EMAIL
# ─────────────────────────────────────────

def send_email(s: Signal) -> bool:
    if not EMAIL_ENABLED or not GMAIL_SENDER:
        return False
    try:
        conf_label = _confidence_label(s.confidence)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = (
            f"[BIST] {_emoji(s.side, s.strength)} {s.symbol} — "
            f"{s.side} ({s.strength}) @ {s.price} TRY | "
            f"Confidence: {s.confidence}% [{conf_label}]"
        )
        msg["From"] = f"BIST Signal Bot <{GMAIL_SENDER}>"
        msg["To"]   = GMAIL_RECIPIENT
        msg.attach(MIMEText(_build_message(s, html=False), "plain", "utf-8"))
        msg.attach(MIMEText(_build_message(s, html=True),  "html",  "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_SENDER, GMAIL_PASSWORD)
            smtp.sendmail(GMAIL_SENDER, GMAIL_RECIPIENT, msg.as_string())
        log.info(f"Email sent: {s.symbol}")
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("Gmail authentication failed! Use an App Password.")
        return False
    except Exception as e:
        log.error(f"Email could not be sent: {e}")
        return False


def send_alert(s: Signal):
    if TELEGRAM_ENABLED:
        send_telegram(s)
    if EMAIL_ENABLED:
        send_email(s)


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
            current = _current_prices.get(symbol, pos.buy_price)
            pos_pnl = (current - pos.buy_price) * pos.quantity
            pos_pnl_str = f"+{pos_pnl:.2f}" if pos_pnl >= 0 else f"{pos_pnl:.2f}"
            # Show latest indicators for open positions
            hist  = _price_history.get(symbol, [])
            rsi_v = compute_rsi(hist)
            mom_v = compute_momentum(hist)
            rsi_s = f"RSI={rsi_v}" if rsi_v is not None else "RSI=n/a"
            mom_s = f"MOM={mom_v:+.1f}%" if mom_v is not None else "MOM=n/a"
            lines.append(
                f"  {symbol}: {pos.quantity} shares | Buy: {pos.buy_price} TRY | "
                f"Current: {current} TRY | P&L: {pos_pnl_str} TRY | {rsi_s} {mom_s}"
            )
    else:
        lines.append("📌 Open Positions: None")
    lines += [
        divider, "📋 Trade Summary:",
        f"  Buys  : {len(buys)} trades",
        f"  Sells : {len(sells)} trades",
        f"  Realized P&L    : {realized_pnl:+.2f} TRY",
        f"  Total Commission: {total_comm:.2f} TRY",
    ]
    if portfolio.trades:
        lines.append(divider)
        lines.append("📝 Trade Details:")
        for t in portfolio.trades:
            pnl_str = f" | P&L: {t.pnl:+.2f} TRY" if t.pnl is not None else ""
            lines.append(
                f"  {t.time} | {t.side:4s} | {t.symbol} | "
                f"{t.quantity} shares @ {t.price} TRY{pnl_str}"
            )
    lines += [divider, "⚠️ This report is simulation data only. Not real trading."]

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
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = (
            f"{'📈' if pnl >= 0 else '📉'} BIST Simulation Report — "
            f"{datetime.now().strftime('%d.%m.%Y')} | "
            f"Total: {total:.2f} TRY ({pnl:+.2f} TRY)"
        )
        msg["From"] = f"BIST Signal Bot <{GMAIL_SENDER}>"
        msg["To"]   = GMAIL_RECIPIENT
        msg.attach(MIMEText(_build_eod_report(html=False), "plain", "utf-8"))
        msg.attach(MIMEText(_build_eod_report(html=True),  "html",  "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_SENDER, GMAIL_PASSWORD)
            smtp.sendmail(GMAIL_SENDER, GMAIL_RECIPIENT, msg.as_string())
        log.info(f"[EOD] Report emailed → {GMAIL_RECIPIENT}")
    except Exception as e:
        log.error(f"[EOD] Email failed: {e}")


def send_eod_telegram():
    if not TELEGRAM_ENABLED or not TELEGRAM_TOKEN:
        return
    try:
        text = _build_eod_report(html=False)
        if len(text) > 4000:
            text = text[:4000] + "\n...(full report sent via email)"
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10,
        )
        log.info("[EOD] Report sent via Telegram.")
    except Exception as e:
        log.error(f"[EOD] Telegram report failed: {e}")


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
            _price_history.pop(sym, None)   # clean up price history too
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

        signal     = generate_signal(symbol, data, stocks)
        vol_ratio  = (data["volume"] / data["avg_vol"]) if data["avg_vol"] > 0 else 0
        warmup     = data.get("vol_warmup", 0)
        warmup_str = "" if warmup >= VOL_WARMUP_SCANS else f" ⏳vol-warmup {warmup}/{VOL_WARMUP_SCANS}"

        prev_price = last_signals[symbol].price if symbol in last_signals else data["price"]
        price_chg  = data["price"] - prev_price
        chg_str    = f" ({price_chg:+.2f})" if price_chg != 0 else ""

        s        = stocks[symbol]
        dist_sup = round(data["price"] - s["strong_support"], 2)
        dist_res = round(s["resistance_1"] - data["price"], 2)

        rsi_str  = f"RSI={signal.rsi}" if signal.rsi is not None else "RSI=⏳"
        mom_str  = f"MOM={signal.momentum:+.1f}%" if signal.momentum is not None else "MOM=⏳"
        conf_str = f"conf={signal.confidence}%[{_confidence_label(signal.confidence)}]" if signal.side != "WAIT" else ""

        log.info(
            f"{symbol}: {signal.price} TRY{chg_str} → {signal.side} ({signal.strength})"
            f" | vol {vol_ratio:.1f}x{warmup_str}"
            f" | sup+{dist_sup:.2f} res-{dist_res:.2f}"
            f" | {rsi_str} {mom_str} trend={signal.auto_trend}[{signal.trend_source}]"
            + (f" | {conf_str}" if conf_str else "")
        )

        check_stop_loss(symbol, signal.price, stocks)

        if signal.side != "WAIT" and signal_changed(symbol, signal):
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

    log.info("=" * 55)
    log.info("  BIST Signal Bot v2.0 — Dynamic Stocks + Estimation")
    log.info(f"  Loaded stocks    : {', '.join(sorted(stocks)) if stocks else 'NONE'}")
    log.info(f"  Stocks folder    : {os.path.abspath(STOCKS_FOLDER)}/")
    log.info(f"  Log file         : {os.path.abspath(LOG_FILE)}")
    log.info(f"  Scan interval    : {SCAN_INTERVAL} min")
    log.info(f"  Vol warmup       : {VOL_WARMUP_SCANS} unique readings required")
    log.info(f"  Price window     : {PRICE_WINDOW} readings max")
    log.info(f"  RSI period       : {RSI_PERIOD} scans")
    log.info(f"  Momentum period  : {MOMENTUM_PERIOD} scans (~{MOMENTUM_PERIOD * SCAN_INTERVAL} min)")
    log.info(f"  EMA short/long   : EMA{EMA_SHORT} / EMA{EMA_LONG}")
    log.info(f"  Starting balance : {portfolio.starting_balance:,.0f} TRY")
    log.info(f"  Telegram : {'ON' if TELEGRAM_ENABLED else 'OFF'}")
    log.info(f"  Email    : {'ON' if EMAIL_ENABLED else 'OFF'}")
    log.info("=" * 55)
    log.info("💡 To add a stock: create stocks/SYMBOL.txt")
    log.info("💡 trend/trend_strength in .txt are now optional — auto-detected via EMA crossover")
    log.info(f"💡 Indicators warm up after {max(RSI_PERIOD, EMA_LONG, MOMENTUM_PERIOD) + 1} scans per symbol")

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
