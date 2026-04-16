"""
main.py — BIST Signal Bot v3.0 Broker Edition
==============================================
Run from inside your project folder:

    cd hisse_v3
    python main.py

All other .py files must be in the same folder as this file.
"""
import logging
import os
import sys
import time
from datetime import datetime

# ── Guarantee all sibling .py files are importable ────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schedule

import settings
from logging_setup   import setup_logging
from stock_loader    import scan_stocks, create_sample_files
from price_fetcher   import fetch_price, _price_history, get_current_prices
from signal_engine   import generate_signal
from indicators      import confidence_label
from portfolio_engine import portfolio, execute_signal, check_stop_loss
from notifier        import (send_signal_alert, send_dca_alert,
                              send_eod_report, build_eod_report)

log = setup_logging()


# ── Session ───────────────────────────────────────────────────────────────────

def session_open() -> bool:
    now = datetime.now()
    return now.weekday() < 5 and settings.SESSION_START <= now.hour < settings.SESSION_END


# ── Scan state ────────────────────────────────────────────────────────────────

_last_signals:     dict = {}
_session_was_open: bool = False
_prev_stock_list:  set  = set()


def _signal_changed(symbol: str, new_sig) -> bool:
    old = _last_signals.get(symbol)
    return old is None or old.side != new_sig.side or old.strength != new_sig.strength


# ── End-of-day ────────────────────────────────────────────────────────────────

def _end_of_day(stocks: dict) -> None:
    log.info("[EOD] Session closed, preparing report...")
    current_prices = get_current_prices()
    log.info("\n" + build_eod_report(portfolio, current_prices, _price_history))
    send_eod_report(portfolio, current_prices, _price_history)
    portfolio.day_start = datetime.now().strftime("%Y-%m-%d")
    portfolio.trades.clear()
    log.info("[EOD] Trade history cleared for next day.")


# ── Main scan ─────────────────────────────────────────────────────────────────

def scan() -> None:
    global _session_was_open, _prev_stock_list

    stocks = scan_stocks()
    if not stocks:
        log.warning("No stocks loaded — check the stocks/ folder.")
        return

    current_list = set(stocks.keys())
    added   = current_list - _prev_stock_list
    removed = _prev_stock_list - current_list
    if added:
        log.info(f"✅ New stocks: {', '.join(sorted(added))}")
    if removed:
        log.info(f"🗑️  Removed: {', '.join(sorted(removed))}")
        for sym in removed:
            _last_signals.pop(sym, None)
            _price_history.pop(sym, None)
    _prev_stock_list = current_list

    is_open = session_open()
    if _session_was_open and not is_open:
        _end_of_day(stocks)
    _session_was_open = is_open

    if not is_open:
        log.info("Session closed, waiting...")
        return

    log.info(f"Scan started — {len(stocks)} stocks: {', '.join(sorted(stocks))}")

    for symbol in sorted(stocks):
        data = fetch_price(symbol)
        if not data:
            log.warning(f"{symbol}: data unavailable, skipping.")
            continue

        signal    = generate_signal(symbol, data, stocks)
        vol_ratio = (data["volume"] / data["avg_vol"]) if data["avg_vol"] > 0 else 0
        warmup    = data.get("vol_warmup", 0)
        warmup_s  = "" if warmup >= settings.VOL_WARMUP_SCANS else f" ⏳warmup {warmup}/{settings.VOL_WARMUP_SCANS}"

        prev_price = _last_signals[symbol].price if symbol in _last_signals else data["price"]
        chg_str    = f" ({data['price'] - prev_price:+.2f})" if data["price"] != prev_price else ""

        s        = stocks[symbol]
        dist_sup = round(data["price"] - s["strong_support"], 2)
        dist_res = round(s["resistance_1"] - data["price"], 2)
        rsi_str  = f"RSI={signal.rsi}" if signal.rsi is not None else "RSI=⏳"
        mom_str  = f"MOM={signal.momentum:+.1f}%" if signal.momentum is not None else "MOM=⏳"
        conf_str = (f"conf={signal.confidence}%[{confidence_label(signal.confidence)}]"
                    if signal.side != "WAIT" else "")
        pos     = portfolio.positions.get(symbol)
        dca_str = (f" | DCA:{pos.tranche_count}/{settings.DCA_MAX_TRANCHES} avg={pos.avg_price:.2f}"
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
                traded = execute_signal(signal, data, stocks)
                if traded and _signal_changed(symbol, signal):
                    send_signal_alert(signal, portfolio)
                elif not traded and _signal_changed(symbol, signal):
                    log.info(f"[SIGNAL] {symbol}: {signal.side} BLOCKED (conf={signal.confidence}%)")
            else:
                pos_before = portfolio.positions[symbol].tranche_count
                execute_signal(signal, data, stocks)
                pos_after = portfolio.positions.get(symbol)
                if pos_after and pos_after.tranche_count > pos_before:
                    send_dca_alert(symbol, pos_after, data["price"],
                                   signal.rsi, signal.auto_trend)

        elif signal.side == "SELL":
            traded = execute_signal(signal, data, stocks)
            if traded and _signal_changed(symbol, signal):
                send_signal_alert(signal, portfolio)

        _last_signals[symbol] = signal

    log.info("Scan complete.")


# ── Startup banner ────────────────────────────────────────────────────────────

def _banner(stocks: dict) -> None:
    log.info("=" * 57)
    log.info("  BIST Signal Bot v3.0 — Broker Edition")
    log.info(f"  Stocks           : {', '.join(sorted(stocks)) if stocks else 'NONE'}")
    log.info(f"  Stocks folder    : {os.path.abspath(settings.STOCKS_FOLDER)}/")
    log.info(f"  Log file         : {os.path.abspath(settings.LOG_FILE)}")
    log.info(f"  Scan interval    : {settings.SCAN_INTERVAL} min")
    log.info(f"  Price provider   : {settings.PRICE_PROVIDER.upper()}"
             + (" ⚠️  volume in shares — recalibrate volume_multiplier"
                if settings.PRICE_PROVIDER == "yfinance" else ""))
    log.info(f"  Vol warmup       : {settings.VOL_WARMUP_SCANS} readings")
    log.info(f"  RSI/MOM/EMA      : {settings.RSI_PERIOD}/{settings.MOMENTUM_PERIOD}/EMA{settings.EMA_SHORT}-EMA{settings.EMA_LONG}")
    log.info(f"  DCA              : {'ON' if settings.DCA_ENABLED else 'OFF'}"
             + (f" | max {settings.DCA_MAX_TRANCHES} tranches | drop≥{settings.DCA_MIN_DROP_PCT}% | RSI>{settings.DCA_RSI_MIN}"
                if settings.DCA_ENABLED else ""))
    log.info(f"  Trailing stop    : {'ON' if settings.TRAILING_STOP_ENABLED else 'OFF'} ({settings.TRAILING_STOP_PCT}%) | breakeven after +{settings.BREAKEVEN_TRIGGER_PCT}%")
    log.info(f"  Partial profit   : {settings.PARTIAL_PROFIT_PCT}% at R2")
    log.info(f"  Min confidence   : {settings.MIN_CONFIDENCE}%")
    log.info(f"  Quiet zone       : first {settings.AVOID_OPEN_MINUTES}m / last {settings.AVOID_CLOSE_MINUTES}m")
    log.info(f"  Dynamic sizing   : {'ON' if settings.DYNAMIC_SIZING else 'OFF'} (15%-40%)")
    log.info(f"  Min R:R          : {settings.MIN_RISK_REWARD}:1")
    log.info(f"  Stop cooldown    : {settings.STOPLOSS_COOLDOWN_MIN} min")
    log.info(f"  Starting balance : {portfolio.starting_balance:,.0f} TRY")
    log.info(f"  Telegram / Email : {'ON' if settings.TELEGRAM_ENABLED else 'OFF'} / {'ON' if settings.EMAIL_ENABLED else 'OFF'}")
    log.info("=" * 57)
    log.info("💡 Add a stock: create stocks/SYMBOL.txt")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    create_sample_files()
    stocks = scan_stocks()
    _banner(stocks)

    now = datetime.now()
    if not session_open():
        if now.weekday() < 5 and now.hour < settings.SESSION_START:
            wake = now.replace(hour=settings.SESSION_START, minute=0, second=5, microsecond=0)
            secs = int((wake - now).total_seconds())
            log.info(f"Market opens at {settings.SESSION_START}:00. Sleeping {secs // 60}m {secs % 60}s...")
            time.sleep(secs)
        else:
            log.info("Session closed. Waiting for next session.")

    scan()
    schedule.every(settings.SCAN_INTERVAL).minutes.do(scan)
    log.info(f"Bot running — scanning every {settings.SCAN_INTERVAL} min. Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)
