"""
settings.py — All configuration. Edit this file or use a .env file.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Notifications ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED  = os.getenv("TELEGRAM_AKTIF", "true").lower() == "true"

GMAIL_SENDER      = os.getenv("GMAIL_GONDEREN", "")
GMAIL_PASSWORD    = os.getenv("GMAIL_SIFRE",    "")
GMAIL_RECIPIENT   = os.getenv("GMAIL_ALICI",    "")
EMAIL_ENABLED     = os.getenv("EMAIL_AKTIF", "true").lower() == "true"

# ── Bot behaviour ──────────────────────────────────────────────────────────────
STOCKS_FOLDER    = os.getenv("HISSELER_KLASOR", "stocks")
SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL_MIN", "10"))
VOL_WARMUP_SCANS = int(os.getenv("VOL_WARMUP_SCANS",  "5"))
SESSION_START    = 10
SESSION_END      = 18
COMMISSION_RATE  = 0.001

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_FILE    = os.getenv("LOG_FILE",    "bist.log")
LOG_MAX_MB  = int(os.getenv("LOG_MAX_MB",  "10"))
LOG_BACKUPS = int(os.getenv("LOG_BACKUPS", "7"))
LOG_LEVEL   = os.getenv("LOG_LEVEL",  "INFO").upper()

# ── Technical indicators ───────────────────────────────────────────────────────
RSI_PERIOD      = int(os.getenv("RSI_PERIOD",      "14"))
MOMENTUM_PERIOD = int(os.getenv("MOMENTUM_PERIOD", "5"))
EMA_SHORT       = int(os.getenv("EMA_SHORT",       "5"))
EMA_LONG        = int(os.getenv("EMA_LONG",        "20"))
PRICE_WINDOW    = int(os.getenv("PRICE_WINDOW",    "50"))

# ── DCA ────────────────────────────────────────────────────────────────────────
DCA_ENABLED      = os.getenv("DCA_ENABLED",  "true").lower() == "true"
DCA_MAX_TRANCHES = min(int(os.getenv("DCA_MAX_TRANCHES", "2")), 5)
DCA_MIN_DROP_PCT = float(os.getenv("DCA_MIN_DROP_PCT", "3.0"))
DCA_RSI_MIN      = float(os.getenv("DCA_RSI_MIN",      "28"))

# ── Broker Edition (v3.0) ──────────────────────────────────────────────────────
TRAILING_STOP_ENABLED = os.getenv("TRAILING_STOP_ENABLED", "true").lower() == "true"
TRAILING_STOP_PCT     = float(os.getenv("TRAILING_STOP_PCT",     "2.0"))
PARTIAL_PROFIT_PCT    = int(os.getenv("PARTIAL_PROFIT_PCT",      "50"))
MIN_CONFIDENCE        = int(os.getenv("MIN_CONFIDENCE",          "40"))
AVOID_OPEN_MINUTES    = int(os.getenv("AVOID_OPEN_MINUTES",      "30"))
AVOID_CLOSE_MINUTES   = int(os.getenv("AVOID_CLOSE_MINUTES",     "30"))
DYNAMIC_SIZING        = os.getenv("DYNAMIC_SIZING", "true").lower() == "true"
MIN_RISK_REWARD       = float(os.getenv("MIN_RISK_REWARD",       "2.0"))
BREAKEVEN_TRIGGER_PCT = float(os.getenv("BREAKEVEN_TRIGGER_PCT", "1.5"))
STOPLOSS_COOLDOWN_MIN = int(os.getenv("STOPLOSS_COOLDOWN_MIN",   "60"))

# ── Price provider ─────────────────────────────────────────────────────────────
_raw = os.getenv("PRICE_PROVIDER", "bigpara").strip().lower()
if _raw not in ("bigpara", "yfinance"):
    print(f"[WARN] Unknown PRICE_PROVIDER={_raw!r}, falling back to bigpara.")
    _raw = "bigpara"
PRICE_PROVIDER: str = _raw
