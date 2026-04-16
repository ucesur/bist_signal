"""
portfolio_engine.py — Trade execution + broker filter logic.
"""
import logging
from datetime import datetime
from typing import Optional, TYPE_CHECKING

import settings
from models import Portfolio, Position, Tranche, Trade

if TYPE_CHECKING:
    from signal_engine import Signal

log = logging.getLogger(__name__)

portfolio           = Portfolio()
_trailing_highs:    dict = {}
_stoploss_cooldown: dict = {}


def _now() -> str:
    return datetime.now().strftime("%H:%M")


def _in_cooldown(symbol: str) -> bool:
    t = _stoploss_cooldown.get(symbol)
    if t is None:
        return False
    elapsed = (datetime.now() - t).total_seconds() / 60
    if elapsed < settings.STOPLOSS_COOLDOWN_MIN:
        log.debug(f"[COOLDOWN] {symbol}: {settings.STOPLOSS_COOLDOWN_MIN - elapsed:.0f} min left")
        return True
    del _stoploss_cooldown[symbol]
    return False


def _in_quiet_zone() -> bool:
    now = datetime.now()
    since_open  = (now.hour - settings.SESSION_START) * 60 + now.minute
    until_close = (settings.SESSION_END - now.hour)   * 60 - now.minute
    if since_open < settings.AVOID_OPEN_MINUTES:
        log.debug(f"[TIME] {settings.AVOID_OPEN_MINUTES - since_open} min until entries allowed")
        return True
    if until_close < settings.AVOID_CLOSE_MINUTES:
        log.debug(f"[TIME] Close in {until_close} min — no new entries")
        return True
    return False


def _calc_rr(price: float, stop: float, target: float) -> float:
    risk = abs(price - stop)
    return round(abs(target - price) / risk, 2) if risk else 0.0


def _dynamic_pct(confidence: int) -> float:
    if not settings.DYNAMIC_SIZING:
        return portfolio.POSITION_SIZE_PCT
    return 0.15 + min(max(confidence - 40, 0), 40) / 40 * 0.25


def _open_position(symbol: str, price: float, reason: str,
                   stocks: dict, confidence: int = 50) -> bool:
    if symbol in portfolio.positions:
        return False
    pct      = _dynamic_pct(confidence)
    allocate = portfolio.cash * pct
    if allocate < price:
        log.info(f"[SIM] {symbol}: Insufficient cash ({portfolio.cash:.2f} TRY).")
        return False
    qty = int(allocate / price)
    if qty == 0:
        return False
    amount = qty * price
    comm   = amount * settings.COMMISSION_RATE
    portfolio.cash -= (amount + comm)
    portfolio.positions[symbol] = Position(
        symbol=symbol, name=stocks[symbol]["name"],
        tranches=[Tranche(price=price, quantity=qty, time=_now())]
    )
    portfolio.trades.append(Trade(
        time=_now(), symbol=symbol, side="BUY", price=price,
        quantity=qty, amount=amount, commission=comm, pnl=None, reason=reason,
    ))
    log.info(f"[SIM] BUY  | {symbol} | {qty} @ {price} TRY | alloc={pct*100:.0f}% | Cash: {portfolio.cash:.2f} TRY")
    _trailing_highs[symbol] = price
    return True


def _add_tranche(symbol: str, price: float, reason: str) -> bool:
    pos = portfolio.positions.get(symbol)
    if not pos:
        return False
    allocate = portfolio.cash * portfolio.POSITION_SIZE_PCT
    if allocate < price:
        log.info(f"[SIM] {symbol}: Insufficient cash for DCA ({portfolio.cash:.2f} TRY).")
        return False
    qty = int(allocate / price)
    if qty == 0:
        return False
    amount = qty * price
    comm   = amount * settings.COMMISSION_RATE
    portfolio.cash -= (amount + comm)
    pos.tranches.append(Tranche(price=price, quantity=qty, time=_now()))
    portfolio.trades.append(Trade(
        time=_now(), symbol=symbol, side="DCA", price=price,
        quantity=qty, amount=amount, commission=comm, pnl=None, reason=reason,
    ))
    log.info(f"[SIM] DCA  | {symbol} | tranche #{pos.tranche_count} | {qty} @ {price} | avg={pos.avg_price:.4f} | Cash: {portfolio.cash:.2f} TRY")
    return True


def _close_position(symbol: str, price: float, reason: str) -> bool:
    pos = portfolio.positions.get(symbol)
    if not pos:
        return False
    qty    = pos.quantity
    amount = qty * price
    comm   = amount * settings.COMMISSION_RATE
    net    = amount - comm
    pnl    = net - pos.total_cost - pos.total_cost * settings.COMMISSION_RATE
    portfolio.cash += net
    del portfolio.positions[symbol]
    _trailing_highs.pop(symbol, None)
    portfolio.trades.append(Trade(
        time=_now(), symbol=symbol, side="SELL", price=price,
        quantity=qty, amount=amount, commission=comm, pnl=pnl, reason=reason,
    ))
    log.info(f"[SIM] SELL | {symbol} | {qty} @ {price} | avg={pos.avg_price:.4f} | P&L: {pnl:+.2f} TRY")
    return True


def _close_partial(symbol: str, price: float, pct: int, reason: str) -> bool:
    pos = portfolio.positions.get(symbol)
    if not pos:
        return False
    sell_qty = max(1, int(pos.quantity * pct / 100))
    if sell_qty >= pos.quantity:
        return _close_position(symbol, price, reason)
    amount     = sell_qty * price
    comm       = amount * settings.COMMISSION_RATE
    cost_basis = sell_qty * pos.avg_price
    pnl        = (amount - comm) - cost_basis - cost_basis * settings.COMMISSION_RATE
    portfolio.cash += (amount - comm)
    remaining, new_tranches = sell_qty, []
    for t in pos.tranches:
        if remaining <= 0:
            new_tranches.append(t)
        elif t.quantity <= remaining:
            remaining -= t.quantity
        else:
            new_tranches.append(Tranche(t.price, t.quantity - remaining, t.time))
            remaining = 0
    pos.tranches = new_tranches or pos.tranches
    portfolio.trades.append(Trade(
        time=_now(), symbol=symbol, side="SELL", price=price,
        quantity=sell_qty, amount=amount, commission=comm, pnl=pnl, reason=reason,
    ))
    log.info(f"[SIM] PARTIAL SELL | {symbol} | {sell_qty} @ {price} | P&L: {pnl:+.2f} TRY | remaining: {pos.quantity}")
    return True


def check_stop_loss(symbol: str, price: float, stocks: dict) -> None:
    pos = portfolio.positions.get(symbol)
    if not pos:
        return
    if settings.TRAILING_STOP_ENABLED:
        if price > _trailing_highs.get(symbol, price):
            _trailing_highs[symbol] = price
        high      = _trailing_highs.get(symbol, price)
        gain_pct  = (high - pos.avg_price) / pos.avg_price * 100
        trail     = round(high * (1 - settings.TRAILING_STOP_PCT / 100), 2)
        stop      = max(pos.avg_price * 1.001, trail) if gain_pct >= settings.BREAKEVEN_TRIGGER_PCT else trail
        fixed     = round(pos.avg_price * (1 - stocks[symbol]["stop_pct"]), 2)
        effective = max(stop, fixed) if gain_pct >= 0 else fixed
    else:
        effective = round(pos.avg_price * (1 - stocks[symbol]["stop_pct"]), 2)

    if price <= effective:
        log.warning(f"[SIM] STOP-LOSS! {symbol} @ {price} (stop={effective}, avg={pos.avg_price:.4f})")
        _close_position(symbol, price, f"Stop-loss ({effective} TRY)")
        _stoploss_cooldown[symbol] = datetime.now()
        _trailing_highs.pop(symbol, None)


def dca_eligible(symbol: str, price: float, signal: "Signal") -> bool:
    if not settings.DCA_ENABLED:
        return False
    pos = portfolio.positions.get(symbol)
    if not pos or pos.tranche_count >= settings.DCA_MAX_TRANCHES:
        return False
    drop = (pos.last_tranche_price - price) / pos.last_tranche_price * 100
    if drop < settings.DCA_MIN_DROP_PCT:
        return False
    if signal.auto_trend == "down" and signal.trend_source != "default":
        return False
    if signal.rsi is not None and signal.rsi < settings.DCA_RSI_MIN:
        return False
    return True


def execute_signal(signal: "Signal", data: dict, stocks: dict) -> bool:
    price, symbol = data["price"], signal.symbol
    check_stop_loss(symbol, price, stocks)

    if signal.side == "BUY" and signal.strength in ("STRONG", "NORMAL", "BREAKOUT"):
        if symbol not in portfolio.positions:
            if signal.confidence < settings.MIN_CONFIDENCE:
                log.info(f"[GATE] {symbol}: conf {signal.confidence}% < {settings.MIN_CONFIDENCE}% — skip")
                return False
            if _in_quiet_zone():
                log.info(f"[GATE] {symbol}: quiet zone — skip")
                return False
            if _in_cooldown(symbol):
                log.info(f"[GATE] {symbol}: cooldown — skip")
                return False
            s  = stocks[symbol]
            rr = _calc_rr(price, round(price * (1 - s["stop_pct"]), 2), s["resistance_1"])
            if rr < settings.MIN_RISK_REWARD:
                log.info(f"[GATE] {symbol}: R:R={rr} < {settings.MIN_RISK_REWARD} — skip")
                return False
            return _open_position(symbol, price, signal.reason, stocks, signal.confidence)
        else:
            if dca_eligible(symbol, price, signal):
                pos    = portfolio.positions[symbol]
                drop   = (pos.last_tranche_price - price) / pos.last_tranche_price * 100
                reason = (f"DCA #{pos.tranche_count + 1} | drop {drop:.1f}% "
                          f"({pos.last_tranche_price}→{price} TRY) | trend={signal.auto_trend} RSI={signal.rsi}")
                return _add_tranche(symbol, price, reason)
            return False

    elif signal.side == "SELL":
        s = stocks[symbol]
        if s["resistance_2"] <= price < s["resistance_3"] and symbol in portfolio.positions:
            pos = portfolio.positions[symbol]
            if pos.tranche_count > 0 and settings.PARTIAL_PROFIT_PCT < 100:
                return _close_partial(symbol, price, settings.PARTIAL_PROFIT_PCT,
                                      f"Partial TP ({settings.PARTIAL_PROFIT_PCT}%) at R2")
        return _close_position(symbol, price, signal.reason)

    return False
