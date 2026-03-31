"""
BIST Stock Signal Bot
======================
Stocks     : Auto-loaded from .txt files in the stocks/ folder
Interval   : 10 minutes (schedule)
Alerts     : Telegram + Gmail (smtplib)
Data       : Bigpara (~15 min delayed, no registration required)
Simulation : 10,000 TRY starting balance, end-of-day email report

To add a new stock:
  Create a stocks/SYMBOL.txt file — the bot picks it up on the next scan.

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
LOG_MAX_MB   = int(os.getenv("LOG_MAX_MB", "10"))      # rotate after 10 MB
LOG_BACKUPS  = int(os.getenv("LOG_BACKUPS", "7"))       # keep 7 rotated files
LOG_LEVEL    = os.getenv("LOG_LEVEL", "INFO").upper()   # INFO or DEBUG

_fmt     = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S")

# Console handler
_console = logging.StreamHandler()
_console.setFormatter(_fmt)
_console.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

# Clear log file on every startup
open(LOG_FILE, "w").close()

# Rotating file handler — new file every LOG_MAX_MB, keeps LOG_BACKUPS old files
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE,
    maxBytes=LOG_MAX_MB * 1024 * 1024,
    backupCount=LOG_BACKUPS,
    encoding="utf-8",
)
_file_handler.setFormatter(_fmt)
_file_handler.setLevel(logging.DEBUG)   # File captures DEBUG too

# Root logger
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

SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL_MIN", "10"))  # minutes between scans
VOL_WARMUP_SCANS = int(os.getenv("VOL_WARMUP_SCANS",  "5"))   # ignore vol signals until N readings
SESSION_START    = 10
SESSION_END      = 18
COMMISSION_RATE  = 0.001   # 0.1% buy + 0.1% sell (standard broker)

# ─────────────────────────────────────────
#  STOCK LOADING — FROM TXT FILES
# ─────────────────────────────────────────

REQUIRED_FIELDS = [
    "name", "strong_support", "mid_support",
    "resistance_1", "resistance_2", "resistance_3",
    "stop_pct", "volume_multiplier",
]

# Fields that stay as strings (not converted to float)
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
        trend             = up
        trend_strength    = weak
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
                    data[key] = value.lower()   # normalise: "UP" → "up"
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

        # trend / trend_strength are optional — default to sideways/weak if absent
        data.setdefault("trend",          "sideways")
        data.setdefault("trend_strength", "weak")

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

name              = Kocaer Steel
strong_support    = 11.00
mid_support       = 11.80
resistance_1      = 12.20
resistance_2      = 12.60
resistance_3      = 14.05
stop_pct          = 0.04
volume_multiplier = 1.5
trend             = sideways
trend_strength    = weak
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
trend             = sideways
trend_strength    = weak
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
trend             = sideways
trend_strength    = weak
""",
}


def create_sample_files():
    """Creates sample stock files if the stocks folder is empty."""
    if not os.path.isdir(STOCKS_FOLDER):
        os.makedirs(STOCKS_FOLDER)
    existing = glob.glob(os.path.join(STOCKS_FOLDER, "*.txt"))
    if existing:
        return  # Files already exist, do nothing
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
    side:       str       # BUY / SELL
    price:      float
    quantity:   int
    amount:     float
    commission: float
    pnl:        Optional[float]   # Filled only on sells
    reason:     str

@dataclass
class Portfolio:
    starting_balance:      float = 10_000.0
    cash:                  float = 10_000.0
    positions:             dict  = field(default_factory=dict)
    trades:                list  = field(default_factory=list)
    day_start:             str   = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    POSITION_SIZE_PCT:     float = 0.30   # Use 30% of portfolio per trade

    def total_value(self, current_prices: dict) -> float:
        """Cash + current value of open positions."""
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
    """Buys stock when a signal fires. Skips if already in position or insufficient cash."""
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
    """Sells the full position on a SELL signal or stop-loss trigger."""
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
    """Checks whether stop-loss is triggered for open positions."""
    if symbol not in portfolio.positions:
        return
    pos        = portfolio.positions[symbol]
    stop_price = round(pos.buy_price * (1 - stocks[symbol]["stop_pct"]), 2)
    if price <= stop_price:
        log.warning(f"[SIM] STOP-LOSS triggered! {symbol} @ {price} TRY (stop: {stop_price} TRY)")
        portfolio_sell(symbol, price, f"Stop-loss ({stop_price} TRY)")


def updateBalance(signal: "Signal", data: dict, stocks: dict):
    """Updates portfolio based on signal type."""
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

BIGPARA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://bigpara.hurriyet.com.tr/",
}


def fetch_price(symbol: str, retries: int = 3) -> Optional[dict]:
    """
    Fetches stock data from Bigpara (~15 min delayed).
    Retries up to 3 times on connection errors (3s, 6s apart).
    Uses 'alis' (bid) during session and 'kapanis' (close) outside session.
    Adds a small random jitter before each request to reduce 401 rate-limiting.
    """
    import random
    time.sleep(random.uniform(0.3, 1.2))   # jitter: spread requests, avoid rate-limit bursts

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
            _current_prices[symbol] = price

            return {
                "symbol":       symbol,
                "price":        price,
                "volume":       volume,
                "avg_vol":      avg_vol,
                "vol_warmup":   warmup_count,   # how many unique vol readings so far
                "change":       change,
                "time":         datetime.now().strftime("%H:%M"),
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
    """Returns (avg_vol, warmup_count) where warmup_count = number of unique readings so far."""
    if symbol not in _volume_history:
        _volume_history[symbol] = []
    history = _volume_history[symbol]
    # Skip stale/repeated volume — Bigpara sometimes returns the same value
    # multiple times in a row when it throttles (401 fallback caches last value).
    # Adding duplicates inflates avg_vol and permanently suppresses signals.
    if new_volume > 0 and (not history or new_volume != history[-1]):
        history.append(new_volume)
    if len(history) > VOLUME_WINDOW:
        history.pop(0)
    avg = int(sum(history) / len(history)) if history else new_volume or 1
    return avg, len(history)


# ─────────────────────────────────────────
#  SIGNAL ENGINE
# ─────────────────────────────────────────

@dataclass
class Signal:
    symbol:       str
    name:         str
    side:         str     # BUY / SELL / WAIT
    strength:     str     # STRONG / NORMAL / BREAKOUT / TAKE PROFIT / NEUTRAL
    price:        float
    reason:       str
    stop:         Optional[float]
    target_1:     Optional[float]
    target_2:     Optional[float]
    target_3:     Optional[float]
    volume_ok:    bool
    time:         str


def generate_signal(symbol: str, data: dict, stocks: dict) -> Signal:
    s          = stocks[symbol]
    price      = data["price"]
    volume     = data["volume"]
    avg_vol    = data["avg_vol"]
    warmup     = data.get("vol_warmup", VOL_WARMUP_SCANS)
    volume_ok  = (volume > avg_vol * s["volume_multiplier"]) and (warmup >= VOL_WARMUP_SCANS)
    vol_ratio  = (volume / avg_vol) if avg_vol > 0 else 0
    log.debug(f"{symbol}: vol_ratio={vol_ratio:.2f} (need {s['volume_multiplier']:.1f}x) warmup={warmup}/{VOL_WARMUP_SCANS}")
    stop       = round(price * (1 - s["stop_pct"]), 2)

    trend      = s.get("trend",          "sideways")   # up | down | sideways
    strength_t = s.get("trend_strength", "weak")       # strong | weak
    trend_info = f"trend={trend}/{strength_t}"

    # Distance to nearest levels — logged at DEBUG for visibility
    dist_sup = round(price - s["strong_support"], 2)
    dist_res = round(s["resistance_1"] - price, 2)
    log.debug(f"{symbol}: +{dist_sup:.2f} above support | {dist_res:.2f} below resistance_1")

    def _wait(reason: str) -> Signal:
        return Signal(symbol, s["name"], "WAIT", "NEUTRAL", price,
                      reason, None, None, None, None, volume_ok, data["time"])

    # ── BUY signals ──────────────────────────────────────────────────────

    if price <= s["strong_support"]:
        # Falling knife guard: strong downtrend → skip entirely
        if trend == "down" and strength_t == "strong":
            return _wait(
                f"Strong support hit BUT strong downtrend — falling knife risk ⚠️ ({trend_info})"
            )
        # Weak downtrend → caution: fire signal but warn
        vol_note   = " + high volume" if volume_ok else " ⚠️ low volume"
        trend_note = " ⚠️ weak downtrend — caution" if trend == "down" else ""
        strength   = "STRONG" if volume_ok else "STRONG (low vol)"
        return Signal(symbol, s["name"], "BUY", strength, price,
                      f"Strong support ({s['strong_support']} TRY){vol_note}{trend_note}",
                      stop, s["resistance_1"], s["resistance_2"], s["resistance_3"],
                      volume_ok, data["time"])

    elif price <= s["mid_support"] and volume_ok:
        # Mid support: blocked in any downtrend (not just strong)
        if trend == "down":
            return _wait(
                f"Mid support + volume BUT downtrend — waiting for trend reversal ({trend_info})"
            )
        return Signal(symbol, s["name"], "BUY", "NORMAL", price,
                      f"Support zone ({s['mid_support']} TRY) + volume confirmation | {trend_info}",
                      stop, s["resistance_1"], s["resistance_2"], None,
                      volume_ok, data["time"])

    elif price > s["resistance_1"] and volume_ok:
        # Breakout: blocked in downtrend (likely false breakout)
        if trend == "down":
            return _wait(
                f"Breakout above R1 BUT downtrend — high false-breakout risk ({trend_info})"
            )
        return Signal(symbol, s["name"], "BUY", "BREAKOUT", price,
                      f"Resistance broken ({s['resistance_1']} TRY) + volume ✅ | {trend_info}",
                      s["mid_support"], s["resistance_2"], s["resistance_3"], None,
                      volume_ok, data["time"])

    # ── SELL signals — never require volume ──────────────────────────────

    elif price >= s["resistance_3"]:
        return Signal(symbol, s["name"], "SELL", "TAKE PROFIT", price,
                      f"3rd target ({s['resistance_3']} TRY) — close full position",
                      None, None, None, None, volume_ok, data["time"])

    elif price >= s["resistance_2"]:
        # Strong uptrend: hold — target_3 is still reachable
        if trend == "up" and strength_t == "strong":
            return _wait(
                f"2nd target hit BUT strong uptrend — holding for 3rd target "
                f"({s['resistance_3']} TRY) ({trend_info})"
            )
        return Signal(symbol, s["name"], "SELL", "TAKE PROFIT", price,
                      f"2nd target ({s['resistance_2']} TRY) — close 50% of position | {trend_info}",
                      None, None, None, None, volume_ok, data["time"])

    # ── WAIT ─────────────────────────────────────────────────────────────
    else:
        return _wait(
            f"Range-bound ({s['mid_support']}–{s['resistance_1']} TRY)"
            f" | vol {vol_ratio:.1f}x | {trend_info}"
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


def _build_message(s: Signal, html: bool = False) -> str:
    emoji      = _emoji(s.side, s.strength)
    volume_str = "✅ High volume" if s.volume_ok else "⚠️ Low volume"
    divider    = "─" * 30
    lines      = [
        f"{emoji} {s.symbol} — {s.name}", divider,
        f"Price      : {s.price} TRY",
        f"Signal     : {s.side} ({s.strength})",
        f"Volume     : {volume_str}",
        f"Reason     : {s.reason}",
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
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[BIST] {_emoji(s.side, s.strength)} {s.symbol} — {s.side} ({s.strength}) @ {s.price} TRY"
        msg["From"]    = f"BIST Signal Bot <{GMAIL_SENDER}>"
        msg["To"]      = GMAIL_RECIPIENT
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
            lines.append(
                f"  {symbol}: {pos.quantity} shares | Buy: {pos.buy_price} TRY | "
                f"Current: {current} TRY | P&L: {pos_pnl_str} TRY"
            )
    else:
        lines.append("📌 Open Positions: None")
    lines += [
        divider, "📋 Trade Summary:",
        f"  Buys  : {len(buys)} trades",
        f"  Sells : {len(sells)} trades",
        f"  Realized P&L  : {realized_pnl:+.2f} TRY",
        f"  Total Commission: {total_comm:.2f} TRY",
    ]
    if portfolio.trades:
        lines.append(divider)
        lines.append("📝 Trade Details:")
        for t in portfolio.trades:
            pnl_str = f" | P&L: {t.pnl:+.2f} TRY" if t.pnl is not None else ""
            lines.append(f"  {t.time} | {t.side:4s} | {t.symbol} | {t.quantity} shares @ {t.price} TRY{pnl_str}")
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
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    return SESSION_START <= now.hour < SESSION_END


last_signals:    dict = {}
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

    # Reload stocks on every scan (picks up new/removed files)
    stocks = scan_stocks()

    if not stocks:
        log.warning("No stocks loaded! Check the stocks/ folder.")
        return

    # Detect added/removed stocks
    current_list = set(stocks.keys())
    added        = current_list - _prev_stock_list
    removed      = _prev_stock_list - current_list
    if added:
        log.info(f"✅ New stock(s) added: {', '.join(sorted(added))}")
    if removed:
        log.info(f"🗑️  Stock(s) removed: {', '.join(sorted(removed))}")
        for s in removed:
            last_signals.pop(s, None)
    _prev_stock_list = current_list

    is_open = session_open()

    # Trigger end-of-day report when session closes
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

        signal = generate_signal(symbol, data, stocks)
        vol_ratio = (data["volume"] / data["avg_vol"]) if data["avg_vol"] > 0 else 0
        warmup    = data.get("vol_warmup", 0)
        warmup_str = "" if warmup >= VOL_WARMUP_SCANS else f" ⏳warmup {warmup}/{VOL_WARMUP_SCANS}"
        # Show price change vs last scan and distance to nearest levels
        prev_price = last_signals[symbol].price if symbol in last_signals else data["price"]
        price_chg  = data["price"] - prev_price
        chg_str    = f" ({price_chg:+.2f})" if price_chg != 0 else ""
        s          = stocks[symbol]
        dist_sup   = round(data["price"] - s["strong_support"], 2)
        dist_res   = round(s["resistance_1"] - data["price"], 2)
        log.info(
            f"{symbol}: {signal.price} TRY{chg_str} → {signal.side} ({signal.strength})"
            f" | vol {vol_ratio:.1f}x{warmup_str}"
            f" | sup+{dist_sup:.2f} res-{dist_res:.2f}"
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

    log.info("=" * 50)
    log.info("  BIST Signal Bot v1.7 — Dynamic Stocks")
    log.info(f"  Loaded stocks   : {', '.join(sorted(stocks)) if stocks else 'NONE'}")
    log.info(f"  Stocks folder   : {os.path.abspath(STOCKS_FOLDER)}/")
    log.info(f"  Log file        : {os.path.abspath(LOG_FILE)}")
    log.info(f"  Scan interval   : {SCAN_INTERVAL} min")
    log.info(f"  Vol warmup      : {VOL_WARMUP_SCANS} unique readings required")
    log.info(f"  Starting balance: {portfolio.starting_balance:,.0f} TRY")
    log.info(f"  Telegram : {'ON' if TELEGRAM_ENABLED else 'OFF'}")
    log.info(f"  Email    : {'ON' if EMAIL_ENABLED else 'OFF'}")
    log.info("=" * 50)
    log.info("💡 To add a stock: create stocks/SYMBOL.txt")

    # Sleep until market opens instead of busy-waiting
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
