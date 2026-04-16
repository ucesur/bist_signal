"""
price_fetcher.py — Bigpara and Yahoo Finance price providers.
"""
import logging
import random
import time
from datetime import datetime
from typing import Optional

import requests
import settings

log = logging.getLogger(__name__)

_price_history:  dict = {}
_volume_history: dict = {}
_current_prices: dict = {}
VOLUME_WINDOW = 20


def get_current_prices() -> dict:
    return _current_prices


def _session_open() -> bool:
    now = datetime.now()
    return now.weekday() < 5 and settings.SESSION_START <= now.hour < settings.SESSION_END


def _parse_volume(v) -> int:
    try:
        return int(float(str(v).replace(".", "").replace(",", ".")))
    except (ValueError, TypeError):
        return 0


def _update_price_history(symbol: str, price: float) -> list:
    h = _price_history.setdefault(symbol, [])
    if not h or price != h[-1]:
        h.append(price)
    if len(h) > settings.PRICE_WINDOW:
        h.pop(0)
    return h


def _update_volume_avg(symbol: str, new_vol: int) -> tuple:
    h = _volume_history.setdefault(symbol, [])
    if new_vol > 0 and (not h or new_vol != h[-1]):
        h.append(new_vol)
    if len(h) > VOLUME_WINDOW:
        h.pop(0)
    avg = int(sum(h) / len(h)) if h else (new_vol or 1)
    return avg, len(h)


# ── Bigpara ───────────────────────────────────────────────────────────────────

_BP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://bigpara.hurriyet.com.tr/",
}


def _fetch_bigpara(symbol: str, retries: int = 3) -> Optional[dict]:
    time.sleep(random.uniform(0.3, 1.2))
    url = f"https://bigpara.hurriyet.com.tr/api/v1/borsa/hisseyuzeysel/{symbol}"
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=_BP_HEADERS, timeout=10)
            r.raise_for_status()
            raw = r.json().get("data", {}).get("hisseYuzeysel", {})
            if not raw:
                log.warning(f"{symbol} [bigpara]: empty response.")
                return None
            bid, close = raw.get("alis"), raw.get("kapanis")
            raw_price = (bid if bid is not None else close) if _session_open() \
                        else (close if close is not None else bid)
            if raw_price is None:
                log.warning(f"{symbol} [bigpara]: price missing.")
                return None
            return {
                "price":  float(str(raw_price).replace(",", ".")),
                "volume": _parse_volume(raw.get("hacimtl") or "0"),
                "change": float(str(raw.get("yuzdedegisim") or "0").replace(",", ".").replace("%", "")),
            }
        except requests.exceptions.RequestException as exc:
            log.warning(f"{symbol} [bigpara] attempt {attempt}/{retries}: {exc}")
            if attempt < retries:
                time.sleep(3 * attempt)
            else:
                log.error(f"{symbol} [bigpara]: failed after {retries} attempts.")
                return None
        except (KeyError, ValueError, TypeError) as exc:
            log.error(f"{symbol} [bigpara] parse error: {exc}")
            return None


# ── Yahoo Finance ──────────────────────────────────────────────────────────────

_YF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _fetch_yfinance(symbol: str, retries: int = 3) -> Optional[dict]:
    time.sleep(random.uniform(0.2, 0.8))
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.IS"
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=_YF_HEADERS,
                             params={"interval": "1m", "range": "1d"}, timeout=10)
            r.raise_for_status()
            body   = r.json()
            result = body.get("chart", {}).get("result")
            if not result:
                log.warning(f"{symbol} [yfinance]: {body.get('chart', {}).get('error')}")
                return None
            meta   = result[0].get("meta", {})
            quotes = result[0].get("indicators", {}).get("quote", [{}])[0]
            price  = meta.get("regularMarketPrice")
            if price is None:
                log.warning(f"{symbol} [yfinance]: price missing.")
                return None
            price      = round(float(price), 4)
            volume     = int(meta.get("regularMarketVolume") or 0)
            prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
            change     = round((price - prev_close) / prev_close * 100, 2) \
                         if prev_close and prev_close > 0 else 0.0
            intraday   = [round(float(c), 4) for c in (quotes.get("close") or []) if c is not None]
            return {"price": price, "volume": volume, "change": change, "intraday_closes": intraday}
        except requests.exceptions.RequestException as exc:
            log.warning(f"{symbol} [yfinance] attempt {attempt}/{retries}: {exc}")
            if attempt < retries:
                time.sleep(3 * attempt)
            else:
                log.error(f"{symbol} [yfinance]: failed after {retries} attempts.")
                return None
        except (KeyError, ValueError, TypeError) as exc:
            log.error(f"{symbol} [yfinance] parse error: {exc}")
            return None


# ── Public entry point ────────────────────────────────────────────────────────

def fetch_price(symbol: str) -> Optional[dict]:
    raw = _fetch_yfinance(symbol) if settings.PRICE_PROVIDER == "yfinance" \
          else _fetch_bigpara(symbol)
    if raw is None:
        return None

    if settings.PRICE_PROVIDER == "yfinance":
        intraday = raw.get("intraday_closes", [])
        if intraday:
            _price_history[symbol] = intraday[-settings.PRICE_WINDOW:]

    avg_vol, warmup = _update_volume_avg(symbol, raw["volume"])
    price_hist      = _update_price_history(symbol, raw["price"])
    _current_prices[symbol] = raw["price"]

    return {
        "symbol":        symbol,
        "price":         raw["price"],
        "volume":        raw["volume"],
        "avg_vol":       avg_vol,
        "vol_warmup":    warmup,
        "change":        raw["change"],
        "time":          datetime.now().strftime("%H:%M"),
        "price_history": price_hist,
    }
