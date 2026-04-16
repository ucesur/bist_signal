"""
signal_engine.py — Signal dataclass + generate_signal().
"""
import logging
from dataclasses import dataclass
from typing import Optional

import settings
from indicators import compute_rsi, compute_momentum, detect_trend, compute_confidence

log = logging.getLogger(__name__)


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
    s         = stocks[symbol]
    price     = data["price"]
    volume    = data["volume"]
    avg_vol   = data["avg_vol"]
    warmup    = data.get("vol_warmup", settings.VOL_WARMUP_SCANS)
    prices    = data.get("price_history", [])

    volume_ok  = volume > avg_vol * s["volume_multiplier"] and warmup >= settings.VOL_WARMUP_SCANS
    vol_ratio  = (volume / avg_vol) if avg_vol > 0 else 0
    stop       = round(price * (1 - s["stop_pct"]), 2)
    rsi        = compute_rsi(prices)
    momentum   = compute_momentum(prices)
    trend, strength_t, trend_src = detect_trend(prices, s.get("trend"), s.get("trend_strength"))
    trend_info = f"trend={trend}/{strength_t}[{trend_src}]"

    def _make(side, strength, reason, sv, t1, t2, t3) -> Signal:
        conf = compute_confidence(side, rsi, volume_ok, trend, momentum)
        return Signal(symbol, s["name"], side, strength, price, reason,
                      sv, t1, t2, t3, volume_ok, data["time"],
                      rsi, momentum, conf, trend, trend_src)

    def _wait(reason: str) -> Signal:
        return Signal(symbol, s["name"], "WAIT", "NEUTRAL", price, reason,
                      None, None, None, None, volume_ok, data["time"],
                      rsi, momentum, 0, trend, trend_src)

    # ── BUY ──────────────────────────────────────────────────────────────────
    if price <= s["strong_support"]:
        if trend == "down" and strength_t == "strong":
            return _wait(f"Strong support BUT strong downtrend — falling knife ⚠️ ({trend_info})")
        rsi_note   = f" ⚠️ RSI={rsi} elevated"   if rsi is not None and rsi > 60  else ""
        vol_note   = " + high volume"             if volume_ok                     else " ⚠️ low volume"
        trend_note = " ⚠️ weak downtrend"         if trend == "down"               else ""
        return _make("BUY", "STRONG" if volume_ok else "STRONG (low vol)",
                     f"Strong support ({s['strong_support']} TRY){vol_note}{trend_note}{rsi_note} | {trend_info}",
                     stop, s["resistance_1"], s["resistance_2"], s["resistance_3"])

    if price <= s["mid_support"] and volume_ok:
        if trend == "down":
            return _wait(f"Mid support + volume BUT downtrend ({trend_info})")
        mom_note = (f" ⚠️ MOM={momentum:+.1f}% dropping" if momentum is not None and momentum < -2.0 else "")
        return _make("BUY", "NORMAL",
                     f"Support zone ({s['mid_support']} TRY) + volume | {trend_info}{mom_note}",
                     stop, s["resistance_1"], s["resistance_2"], None)

    if price > s["resistance_1"] and volume_ok:
        if trend == "down":
            return _wait(f"Breakout BUT downtrend — false-breakout risk ({trend_info})")
        rsi_note = (f" ⚠️ RSI={rsi} overbought" if rsi is not None and rsi > 75 else "")
        return _make("BUY", "BREAKOUT",
                     f"Resistance broken ({s['resistance_1']} TRY) + volume ✅ | {trend_info}{rsi_note}",
                     s["mid_support"], s["resistance_2"], s["resistance_3"], None)

    # ── SELL ─────────────────────────────────────────────────────────────────
    if price >= s["resistance_3"]:
        return _make("SELL", "TAKE PROFIT",
                     f"3rd target ({s['resistance_3']} TRY) — close full position",
                     None, None, None, None)

    if price >= s["resistance_2"]:
        if trend == "up" and strength_t == "strong" and (rsi is None or rsi < 75):
            return _wait(f"2nd target BUT strong uptrend — holding for R3 ({trend_info})")
        return _make("SELL", "TAKE PROFIT",
                     f"2nd target ({s['resistance_2']} TRY) — close 50% | {trend_info}",
                     None, None, None, None)

    # ── WAIT ──────────────────────────────────────────────────────────────────
    rsi_str = f"RSI={rsi}" if rsi is not None else "RSI=warmup"
    mom_str = f"MOM={momentum:+.1f}%" if momentum is not None else "MOM=warmup"
    return _wait(f"Range-bound ({s['mid_support']}–{s['resistance_1']} TRY) | vol {vol_ratio:.1f}x | {rsi_str} | {mom_str} | {trend_info}")
