"""
indicators.py — Stateless technical indicator functions.
"""
from typing import Optional
import settings


def compute_rsi(prices: list, period: int = None) -> Optional[float]:
    period = period or settings.RSI_PERIOD
    if len(prices) < period + 1:
        return None
    deltas   = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
    recent   = deltas[-period:]
    avg_gain = sum(max(d, 0)      for d in recent) / period
    avg_loss = sum(abs(min(d, 0)) for d in recent) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 1)


def compute_ema(prices: list, period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    k   = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 4)


def compute_momentum(prices: list, period: int = None) -> Optional[float]:
    period = period or settings.MOMENTUM_PERIOD
    if len(prices) < period + 1:
        return None
    base = prices[-(period + 1)]
    return round((prices[-1] - base) / base * 100, 2) if base != 0 else None


def detect_trend(prices: list, manual_trend: Optional[str],
                 manual_strength: Optional[str]) -> tuple:
    if manual_trend is not None:
        return manual_trend, manual_strength or "weak", "manual"
    ema_s = compute_ema(prices, settings.EMA_SHORT)
    ema_l = compute_ema(prices, settings.EMA_LONG)
    if ema_s is None or ema_l is None:
        return "sideways", "weak", "default"
    diff = (ema_s - ema_l) / ema_l * 100
    if diff > 3.0:  return "up",      "strong", "ema"
    if diff > 1.5:  return "up",      "weak",   "ema"
    if diff < -3.0: return "down",    "strong", "ema"
    if diff < -1.5: return "down",    "weak",   "ema"
    return "sideways", "weak", "ema"


def compute_confidence(side: str, rsi: Optional[float], volume_ok: bool,
                        trend: str, momentum: Optional[float]) -> int:
    score = 0
    if volume_ok:
        score += 20
    if rsi is not None:
        if side == "BUY":
            score += 20 if rsi < 30 else (8 if rsi <= 50 else 0)
        elif side == "SELL":
            score += 20 if rsi > 70 else (8 if rsi >= 50 else 0)
    if side == "BUY":
        score += 20 if trend == "up" else (8 if trend == "sideways" else 0)
    elif side == "SELL":
        score += 20 if trend == "down" else (8 if trend == "sideways" else 0)
    if momentum is not None:
        if side == "BUY":
            score += 20 if momentum < -0.5 else (8 if abs(momentum) <= 0.5 else 0)
        elif side == "SELL":
            score += 20 if momentum > 0.5 else (8 if abs(momentum) <= 0.5 else 0)
    return min(score, 100)


def confidence_label(score: int) -> str:
    if score >= 75: return "HIGH"
    if score >= 50: return "MODERATE"
    if score >= 25: return "LOW"
    return "VERY LOW"
