"""
BIST Technical Analysis Updater
=================================
Uses Claude Code + TradingView to automatically update
.txt files in the stocks/ folder.

How it works:
  1. Scans all .txt files in the stocks/ folder
  2. Runs Claude Code to perform a TradingView analysis for each stock
  3. Writes the new support/resistance levels back to the .txt file
  4. bist_signal_bot.py picks up the new levels on its next scan

Setup:
  pip install requests schedule python-dotenv
  npm install -g @anthropic-ai/claude-code   (Claude Code)

Usage:
  python analysis_updater.py                 # Run once
  python analysis_updater.py --loop          # Auto-run every 2 hours
  python analysis_updater.py --symbol KCAER  # Analyse a single stock
"""

import os
import glob
import json
import argparse
import logging
import schedule
import time
import subprocess
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STOCKS_FOLDER  = os.getenv("HISSELER_KLASOR", "stocks")
SESSION_START  = 10
SESSION_END    = 18

# ─────────────────────────────────────────
#  CLAUDE CODE PROMPT TEMPLATE
# ─────────────────────────────────────────

ANALYSIS_PROMPT_TEMPLATE = """
Perform a technical analysis of BIST:{symbol} on TradingView.

Steps:
1. Open https://www.tradingview.com/chart/ in Chrome
2. Search for "BIST:{symbol}" and open the chart
3. Analyse the following timeframes in order: Weekly (1W), Daily (1D), Hourly (1H)
4. Take a screenshot at each timeframe and examine it

Return your analysis ONLY in the following JSON format (nothing else):

{{
  "symbol": "{symbol}",
  "name": "Company name",
  "strong_support": 0.00,
  "mid_support": 0.00,
  "resistance_1": 0.00,
  "resistance_2": 0.00,
  "resistance_3": 0.00,
  "stop_pct": 0.04,
  "volume_multiplier": 1.5,
  "trend": "UP/DOWN/SIDEWAYS",
  "pattern": "Detected chart pattern or NONE",
  "summary": "2-3 sentence technical outlook",
  "updated": "{date}"
}}

Rules:
- All price values must come from the actual TradingView chart
- strong_support: Strongest support level (confirmed by session closes)
- mid_support: Second support level
- resistance_1/2/3: Resistance levels ordered nearest to furthest
- stop_pct: Between 0.03-0.06 based on volatility
- volume_multiplier: Volume filter, usually 1.5
- Return JSON only — do not wrap in markdown code blocks
"""


# ─────────────────────────────────────────
#  CLAUDE CODE RUNNER
# ─────────────────────────────────────────

def run_claude_code(prompt: str, timeout: int = 180) -> Optional[str]:
    """
    Runs Claude Code via subprocess with the --chrome flag.
    Returns the output as a string, or None on failure.
    """
    try:
        result = subprocess.run(
            ["claude", "--chrome", "--print", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
        )
        if result.returncode != 0:
            log.error(f"Claude Code exited with code {result.returncode}: {result.stderr[:200]}")
            return None
        return result.stdout.strip()
    except FileNotFoundError:
        log.error("'claude' command not found. Run: npm install -g @anthropic-ai/claude-code")
        return None
    except subprocess.TimeoutExpired:
        log.error(f"Claude Code timed out ({timeout}s).")
        return None
    except Exception as e:
        log.error(f"Claude Code execution error: {e}")
        return None


# ─────────────────────────────────────────
#  JSON PARSER
# ─────────────────────────────────────────

def parse_json(response: str) -> Optional[dict]:
    """
    Extracts JSON from Claude's response.
    Strips markdown code fences if present.
    """
    if not response:
        return None
    clean = response.strip()
    if "```" in clean:
        lines = clean.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        clean = "\n".join(lines).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("{")
        end   = clean.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(clean[start:end])
            except json.JSONDecodeError:
                pass
        log.error(f"Could not parse JSON: {clean[:200]}")
        return None


# ─────────────────────────────────────────
#  TXT FILE WRITER
# ─────────────────────────────────────────

def update_txt(symbol: str, data: dict) -> bool:
    """
    Writes the analysis result to stocks/SYMBOL.txt.
    Backs up the old file as .bak before overwriting.
    """
    file_path = os.path.join(STOCKS_FOLDER, f"{symbol}.txt")

    # Backup existing file
    if os.path.exists(file_path):
        backup_path = file_path.replace(".txt", ".bak")
        try:
            with open(file_path) as f:
                old_content = f.read()
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write(old_content)
        except Exception as e:
            log.warning(f"Backup failed: {e}")

    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    content = f"""\
# {data.get('name', symbol)} — Technical Levels
# Last updated    : {timestamp}
# Trend           : {data.get('trend', '—')}
# Pattern         : {data.get('pattern', '—')}
# Summary         : {data.get('summary', '—')}

name              = {data.get('name', symbol)}
strong_support    = {data.get('strong_support', 0):.2f}
mid_support       = {data.get('mid_support', 0):.2f}
resistance_1      = {data.get('resistance_1', 0):.2f}
resistance_2      = {data.get('resistance_2', 0):.2f}
resistance_3      = {data.get('resistance_3', 0):.2f}
stop_pct          = {data.get('stop_pct', 0.04):.2f}
volume_multiplier = {data.get('volume_multiplier', 1.5):.1f}
"""

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        log.info(f"✅ {file_path} updated.")
        return True
    except Exception as e:
        log.error(f"Could not write {file_path}: {e}")
        return False


# ─────────────────────────────────────────
#  SINGLE STOCK ANALYSIS
# ─────────────────────────────────────────

def analyse_stock(symbol: str) -> bool:
    """Runs a Claude Code analysis for one stock and updates its TXT file."""
    log.info(f"[ANALYSIS] Analysing {symbol}...")

    date   = datetime.now().strftime("%d.%m.%Y %H:%M")
    prompt = ANALYSIS_PROMPT_TEMPLATE.format(symbol=symbol, date=date)

    response = run_claude_code(prompt, timeout=180)
    if not response:
        log.error(f"[ANALYSIS] {symbol}: No response from Claude Code.")
        return False

    data = parse_json(response)
    if not data:
        log.error(f"[ANALYSIS] {symbol}: JSON parsing failed.")
        log.debug(f"Raw response: {response[:500]}")
        return False

    # Basic validation
    required = ["strong_support", "mid_support", "resistance_1", "resistance_2", "resistance_3"]
    missing  = [f for f in required if f not in data or data[f] == 0]
    if missing:
        log.error(f"[ANALYSIS] {symbol}: Missing or zero fields: {missing}")
        return False

    log.info(
        f"[ANALYSIS] {symbol}: support={data['strong_support']}/{data['mid_support']} "
        f"| resistance={data['resistance_1']}/{data['resistance_2']}/{data['resistance_3']} "
        f"| trend={data.get('trend', '?')}"
    )

    return update_txt(symbol, data)


# ─────────────────────────────────────────
#  ANALYSE ALL STOCKS
# ─────────────────────────────────────────

def analyse_all():
    """Analyses every .txt file in the stocks/ folder."""
    if not session_suitable():
        log.info("[ANALYSIS] Outside trading hours, analysis skipped.")
        return

    files   = sorted(glob.glob(os.path.join(STOCKS_FOLDER, "*.txt")))
    symbols = [
        os.path.splitext(os.path.basename(f))[0].upper()
        for f in files
        if not f.endswith(".bak")
    ]

    if not symbols:
        log.warning("[ANALYSIS] No stock files found.")
        return

    log.info(f"[ANALYSIS] Analysing {len(symbols)} stocks: {', '.join(symbols)}")

    success = 0
    for symbol in symbols:
        if analyse_stock(symbol):
            success += 1
        time.sleep(5)   # Brief pause between stocks to avoid overloading TradingView

    log.info(f"[ANALYSIS] Done: {success}/{len(symbols)} stocks updated.")


def session_suitable() -> bool:
    """Returns True if it's a suitable time to run analysis (30 min before open or during session)."""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    return (now.hour == 9 and now.minute >= 30) or (SESSION_START <= now.hour < SESSION_END)


# ─────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="BIST Technical Analysis Updater")
    parser.add_argument("--loop",   action="store_true", help="Run automatically every 2 hours")
    parser.add_argument("--now",    action="store_true", help="Run once immediately")
    parser.add_argument("--symbol", type=str,            help="Analyse a single stock only")
    args = parser.parse_args()

    if not os.path.isdir(STOCKS_FOLDER):
        os.makedirs(STOCKS_FOLDER)
        log.warning(f"'{STOCKS_FOLDER}/' folder created. Add stock files before running.")
        return

    log.info("=" * 50)
    log.info("  BIST Technical Analysis Updater")
    log.info(f"  Folder : {os.path.abspath(STOCKS_FOLDER)}/")
    log.info(f"  Mode   : {'Loop (every 2h)' if args.loop else 'Single run'}")
    log.info("=" * 50)

    if args.symbol:
        analyse_stock(args.symbol.upper())
        return

    if args.loop:
        analyse_all()   # Run immediately on start

        schedule.every(2).hours.do(analyse_all)
        schedule.every().day.at("09:30").do(analyse_all)
        schedule.every().day.at("13:00").do(analyse_all)

        log.info("Loop started. Schedule: 09:30, 13:00, and every 2 hours.")
        log.info("Press Ctrl+C to stop.")
        while True:
            schedule.run_pending()
            time.sleep(30)
    else:
        analyse_all()


if __name__ == "__main__":
    main()
