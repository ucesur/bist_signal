"""
stock_loader.py — Reads per-symbol .txt files from the stocks/ folder.
"""
import glob
import logging
import os
from typing import Optional

import settings

log = logging.getLogger(__name__)

REQUIRED_FIELDS = [
    "name", "strong_support", "mid_support",
    "resistance_1", "resistance_2", "resistance_3",
    "stop_pct", "volume_multiplier",
]
STRING_FIELDS = {"name", "trend", "trend_strength"}

_SAMPLES = {
    "KCAER.txt": """\
# Kocaer Steel — Technical Levels
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


def load_stock(file_path: str) -> Optional[tuple]:
    symbol = os.path.splitext(os.path.basename(file_path))[0].upper()
    data: dict = {}
    try:
        with open(file_path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    log.warning(f"{file_path}:{line_no} — skipped: {line!r}")
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip().lower(), value.strip()
                if key in STRING_FIELDS:
                    data[key] = value.lower()
                else:
                    try:
                        data[key] = float(value.replace(",", "."))
                    except ValueError:
                        log.warning(f"{file_path}:{line_no} — bad float for '{key}': {value!r}")
                        return None

        missing = [f for f in REQUIRED_FIELDS if f not in data]
        if missing:
            log.error(f"{file_path} — Missing fields: {missing}")
            return None

        data.setdefault("trend", None)
        data.setdefault("trend_strength", None)
        return symbol, data

    except FileNotFoundError:
        log.error(f"{file_path} — File not found.")
        return None
    except Exception as exc:
        log.error(f"{file_path} — Read error: {exc}")
        return None


def scan_stocks() -> dict:
    if not os.path.isdir(settings.STOCKS_FOLDER):
        os.makedirs(settings.STOCKS_FOLDER)
        log.info(f"Created stocks folder: '{settings.STOCKS_FOLDER}/'")
    stocks = {}
    for file in sorted(glob.glob(os.path.join(settings.STOCKS_FOLDER, "*.txt"))):
        result = load_stock(file)
        if result:
            stocks[result[0]] = result[1]
    return stocks


def create_sample_files() -> None:
    if not os.path.isdir(settings.STOCKS_FOLDER):
        os.makedirs(settings.STOCKS_FOLDER)
    if glob.glob(os.path.join(settings.STOCKS_FOLDER, "*.txt")):
        return
    for filename, content in _SAMPLES.items():
        path = os.path.join(settings.STOCKS_FOLDER, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        log.info(f"Sample file created: {path}")
