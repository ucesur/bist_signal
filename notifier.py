"""
notifier.py — Message building and dispatch (Telegram + Gmail).
"""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests

import settings
from models import Position
from signal_engine import Signal
from indicators import confidence_label, compute_rsi, compute_momentum

log = logging.getLogger(__name__)

_DIV = "─" * 30


# ── Formatting helpers ────────────────────────────────────────────────────────

def _emoji(side: str, strength: str) -> str:
    if side == "BUY"  and strength == "STRONG":   return "🟢🔥"
    if side == "BUY"  and strength == "NORMAL":   return "🟢"
    if side == "BUY"  and strength == "BREAKOUT": return "🚀"
    if side == "SELL":                            return "🔴"
    return "⏳"


def _conf_bar(score: int) -> str:
    f = round(score / 12.5)
    return "█" * f + "░" * (8 - f) + f" {score}%"


def _html(lines: list, size: str = "14px") -> str:
    body = "".join(f"<p>{l}</p>" for l in lines)
    return f'<html><body style="font-family:monospace;font-size:{size};">{body}</body></html>'


# ── Message builders ──────────────────────────────────────────────────────────

def build_signal_message(signal: Signal, portfolio, html: bool = False) -> str:
    emoji    = _emoji(signal.side, signal.strength)
    vol_str  = "✅ High volume" if signal.volume_ok else "⚠️ Low volume"
    rsi_str  = f"{signal.rsi}" if signal.rsi is not None else "warming up..."
    mom_str  = f"{signal.momentum:+.2f}%" if signal.momentum is not None else "warming up..."
    clabel   = confidence_label(signal.confidence)
    cbar     = _conf_bar(signal.confidence)
    trend_lbl = signal.auto_trend.upper()
    if signal.trend_source == "manual":    trend_lbl += " (manual)"
    elif signal.trend_source == "default": trend_lbl += " (warmup)"

    dca_line = ""
    pos = portfolio.positions.get(signal.symbol)
    if pos and settings.DCA_ENABLED:
        rem      = settings.DCA_MAX_TRANCHES - pos.tranche_count
        dca_line = f"📦 DCA       : tranche {pos.tranche_count}/{settings.DCA_MAX_TRANCHES} | {rem} remaining"

    lines = [
        f"{emoji} {signal.symbol} — {signal.name}", _DIV,
        f"Price      : {signal.price} TRY",
        f"Quantity   : {pos.quantity if pos else '—'}",
        f"Signal     : {signal.side} ({signal.strength})",
        f"Volume     : {vol_str}",
        f"Reason     : {signal.reason}",
        _DIV,
        f"📊 RSI       : {rsi_str}",
        f"📈 Momentum  : {mom_str}",
        f"🔀 Trend     : {trend_lbl}",
        f"🎯 Confidence: {cbar} [{clabel}]",
    ]
    if dca_line:
        lines.append(dca_line)
    lines.append(_DIV)
    if signal.stop:     lines.append(f"Stop-Loss  : {signal.stop} TRY")
    if signal.target_1: lines.append(f"Target 1   : {signal.target_1} TRY")
    if signal.target_2: lines.append(f"Target 2   : {signal.target_2} TRY")
    if signal.target_3: lines.append(f"Target 3   : {signal.target_3} TRY")
    lines += [_DIV, f"Time: {signal.time}", "⚠️ Not investment advice."]

    if html:
        return _html(lines)
    return f"{emoji} *{signal.symbol} — {signal.name}*\n" + "\n".join(lines[1:])


def build_dca_message(symbol: str, pos: Position, price: float,
                       rsi: Optional[float], trend: str, html: bool = False) -> str:
    drop = (pos.tranches[-2].price - price) / pos.tranches[-2].price * 100
    lines = [
        f"📉➕ DCA ADD — {symbol} — {pos.name}", _DIV,
        f"Tranche    : #{pos.tranche_count} of {settings.DCA_MAX_TRANCHES}",
        f"Price      : {price} TRY  (↓{drop:.1f}% from last tranche)",
        f"Avg cost   : {pos.avg_price:.4f} TRY",
        f"Total qty  : {pos.quantity} shares",
        _DIV,
        f"RSI        : {rsi if rsi is not None else 'n/a'}",
        f"Trend      : {trend.upper()}",
        _DIV, "Tranche breakdown:",
    ]
    for i, t in enumerate(pos.tranches, 1):
        lines.append(f"  #{i}: {t.quantity} shares @ {t.price} TRY  [{t.time}]")
    lines += [_DIV, "⚠️ Not investment advice."]
    return _html(lines) if html else "\n".join(lines)


def build_eod_report(portfolio, current_prices: dict, price_history: dict,
                      html: bool = False) -> str:
    from datetime import datetime
    div          = "─" * 36
    today        = datetime.now().strftime("%d.%m.%Y")
    total        = portfolio.total_value(current_prices)
    pnl          = portfolio.total_pnl(current_prices)
    pnl_pct      = portfolio.pnl_pct(current_prices)
    pnl_str      = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
    buys         = [t for t in portfolio.trades if t.side == "BUY"]
    dcas         = [t for t in portfolio.trades if t.side == "DCA"]
    sells        = [t for t in portfolio.trades if t.side == "SELL"]
    total_comm   = sum(t.commission for t in portfolio.trades)
    realized_pnl = sum(t.pnl for t in sells if t.pnl is not None)

    lines = [
        f"📊 BIST SIMULATION REPORT — {today}", div,
        f"💰 Starting  : {portfolio.starting_balance:>10.2f} TRY",
        f"💼 Total     : {total:>10.2f} TRY",
        f"{'📈' if pnl >= 0 else '📉'} P&L      : {pnl_str:>10} TRY  ({pnl_pct:+.2f}%)",
        div, f"🏦 Cash      : {portfolio.cash:>10.2f} TRY",
    ]

    if portfolio.positions:
        lines.append("📌 Open Positions:")
        for sym, pos in portfolio.positions.items():
            cur     = current_prices.get(sym, pos.avg_price)
            pp      = (cur - pos.avg_price) * pos.quantity
            pp_str  = f"+{pp:.2f}" if pp >= 0 else f"{pp:.2f}"
            hist    = price_history.get(sym, [])
            rsi_s   = f"RSI={compute_rsi(hist)}" if compute_rsi(hist) is not None else "RSI=n/a"
            mom_v   = compute_momentum(hist)
            mom_s   = f"MOM={mom_v:+.1f}%" if mom_v is not None else "MOM=n/a"
            lines.append(f"  {sym} [{pos.tranche_count} tranche(s)] | qty={pos.quantity} | avg={pos.avg_price:.4f} | now={cur} | P&L: {pp_str} | {rsi_s} {mom_s}")
            for i, t in enumerate(pos.tranches, 1):
                tp = (cur - t.price) * t.quantity
                lines.append(f"    #{i}: {t.quantity} @ {t.price} [{t.time}] | P&L: {tp:+.2f} TRY")
    else:
        lines.append("📌 Open Positions: None")

    lines += [
        div, "📋 Trade Summary:",
        f"  Buys  : {len(buys)}  DCAs: {len(dcas)}  Sells: {len(sells)}",
        f"  Realized P&L    : {realized_pnl:+.2f} TRY",
        f"  Total Commission: {total_comm:.2f} TRY",
    ]
    if portfolio.trades:
        lines += [div, "📝 Trade Details:"]
        for t in portfolio.trades:
            pnl_s = f" | P&L: {t.pnl:+.2f} TRY" if t.pnl is not None else ""
            lines.append(f"  {t.time} | {t.side:4s} | {t.symbol} | {t.quantity} @ {t.price} TRY{pnl_s}")
    lines += [div, "⚠️ Simulation data only. Not real trading."]

    if html:
        body = "".join(
            f"<p style='color:{'green' if '+' in l else ('red' if l.strip().startswith('-') else 'inherit')}'>{l}</p>"
            for l in lines
        )
        return f'<html><body style="font-family:monospace;font-size:13px;background:#f9f9f9;padding:20px;">{body}</body></html>'
    return "\n".join(lines)


# ── Transports ────────────────────────────────────────────────────────────────

def _telegram(text: str) -> bool:
    if not settings.TELEGRAM_ENABLED or not settings.TELEGRAM_TOKEN:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as exc:
        log.error(f"Telegram error: {exc}")
        return False


def _email(subject: str, plain: str, html_body: str) -> bool:
    if not settings.EMAIL_ENABLED or not settings.GMAIL_SENDER:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"BIST Signal Bot <{settings.GMAIL_SENDER}>"
        msg["To"]      = settings.GMAIL_RECIPIENT
        msg.attach(MIMEText(plain,     "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html",  "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(settings.GMAIL_SENDER, settings.GMAIL_PASSWORD)
            smtp.sendmail(settings.GMAIL_SENDER, settings.GMAIL_RECIPIENT, msg.as_string())
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("Gmail auth failed — use an App Password.")
        return False
    except Exception as exc:
        log.error(f"Email error: {exc}")
        return False


# ── Public send functions ─────────────────────────────────────────────────────

def send_signal_alert(signal: Signal, portfolio) -> None:
    plain = build_signal_message(signal, portfolio, html=False)
    html  = build_signal_message(signal, portfolio, html=True)
    if settings.TELEGRAM_ENABLED and _telegram(plain):
        log.info(f"Telegram sent: {signal.symbol}")
    if settings.EMAIL_ENABLED:
        subj = (f"[BIST] {_emoji(signal.side, signal.strength)} {signal.symbol} — "
                f"{signal.side} ({signal.strength}) @ {signal.price} TRY | "
                f"Conf: {signal.confidence}% [{confidence_label(signal.confidence)}]")
        if _email(subj, plain, html):
            log.info(f"Email sent: {signal.symbol}")


def send_dca_alert(symbol: str, pos: Position, price: float,
                    rsi: Optional[float], trend: str) -> None:
    plain = build_dca_message(symbol, pos, price, rsi, trend, html=False)
    html  = build_dca_message(symbol, pos, price, rsi, trend, html=True)
    subj  = f"[BIST] 📉➕ DCA #{pos.tranche_count} — {symbol} @ {price} TRY | avg={pos.avg_price:.4f}"
    if settings.TELEGRAM_ENABLED and _telegram(plain):
        log.info(f"DCA Telegram: {symbol} tranche #{pos.tranche_count}")
    if settings.EMAIL_ENABLED and _email(subj, plain, html):
        log.info(f"DCA Email: {symbol} tranche #{pos.tranche_count}")


def send_eod_report(portfolio, current_prices: dict, price_history: dict) -> None:
    from datetime import datetime
    total = portfolio.total_value(current_prices)
    pnl   = portfolio.total_pnl(current_prices)
    plain = build_eod_report(portfolio, current_prices, price_history, html=False)
    html  = build_eod_report(portfolio, current_prices, price_history, html=True)
    subj  = (f"{'📈' if pnl >= 0 else '📉'} BIST Report — "
             f"{datetime.now().strftime('%d.%m.%Y')} | Total: {total:.2f} TRY ({pnl:+.2f})")
    if settings.TELEGRAM_ENABLED:
        text = plain if len(plain) <= 4000 else plain[:4000] + "\n...(full report via email)"
        if _telegram(text):
            log.info("[EOD] Telegram sent.")
    if settings.EMAIL_ENABLED and _email(subj, plain, html):
        log.info(f"[EOD] Email sent → {settings.GMAIL_RECIPIENT}")
