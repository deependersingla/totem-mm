#!/usr/bin/env python3
"""
Excel Converter - Converts live_odds.txt to Excel with probabilities

Reads the live_odds.txt file and converts it to an Excel file with:
- Time (IST)
- Betfair Australia % (probability)
- Betfair Oman % (probability)
- Polymarket Australia % (probability)
- Polymarket Oman % (probability)

Runs continuously, updating Excel every 30 seconds.

Usage:
    python scripts/convert_to_excel.py [--input FILE] [--output FILE] [--interval SECONDS]
"""

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

try:
    import pandas as pd
except ImportError:
    print("ERROR: pandas not installed. Install with: pip install pandas openpyxl")
    sys.exit(1)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def parse_betfair_odds(odds_str: str) -> Optional[float]:
    """Parse Betfair odds string like '1.01/1.02' and return mid probability %.
    
    Converts odds to probability: prob = 1/odds
    Returns average probability as percentage.
    """
    if not odds_str or odds_str == "N/A":
        return None
    
    try:
        # Split back/lay odds
        parts = odds_str.split("/")
        if len(parts) != 2:
            return None
        
        back_str = parts[0].strip()
        lay_str = parts[1].strip()
        
        # Handle cases like "1.01/-" or "-/1.02"
        back = float(back_str) if back_str != "-" else None
        lay = float(lay_str) if lay_str != "-" else None
        
        # Convert odds to probabilities
        probs = []
        if back is not None:
            probs.append(1.0 / back)
        if lay is not None:
            probs.append(1.0 / lay)
        
        if not probs:
            return None
        
        # Return average probability as percentage
        avg_prob = sum(probs) / len(probs)
        return avg_prob * 100.0
        
    except (ValueError, ZeroDivisionError):
        return None


def parse_polymarket_odds(odds_str: str) -> Optional[float]:
    """Parse Polymarket odds string like '0.001000/0.999000' and return probability %.
    
    Polymarket prices are already probabilities (0-1 scale).
    
    Strategy: Use ASK price (sell side) as it represents what sellers are willing
    to accept, which is closer to the true probability. If no ask, use bid.
    
    Note: Polymarket UI shows volume-weighted mid-price from order book depth.
    Since we only have top-of-book, using ask price is the best approximation.
    """
    if not odds_str or odds_str == "N/A":
        return None
    
    try:
        # Split bid/ask
        parts = odds_str.split("/")
        if len(parts) != 2:
            return None
        
        bid_str = parts[0].strip()
        ask_str = parts[1].strip()
        
        # Handle cases like "0.001000/-"
        bid = float(bid_str) if bid_str != "-" else None
        ask = float(ask_str) if ask_str != "-" else None
        
        # Use ask price (sell side) - represents what sellers think it's worth
        # This is closer to Polymarket UI's displayed price
        prob = ask if ask is not None else bid
        
        if prob is None:
            return None
        
        # Return as percentage
        return prob * 100.0
        
    except ValueError:
        return None


def parse_line(line: str) -> Optional[dict]:
    """Parse a line from live_odds.txt and extract data.
    
    Format: "2026-02-20 19:24:19  | Betfair Aus: 1.01/1.02 | Betfair Oman: 75.00/80.00 | Poly Aus: 0.001000/0.999000 | Poly Oman: 0.001000/-"
    """
    line = line.strip()
    if not line or line.startswith("IST Time") or line.startswith("-"):
        return None
    
    try:
        # Split by pipe separator
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            return None
        
        # Extract time (first part)
        time_str = parts[0].strip()
        
        # Extract odds from each part
        betfair_aus_str = None
        betfair_oman_str = None
        poly_aus_str = None
        poly_oman_str = None
        
        for part in parts[1:]:
            if "Betfair Aus:" in part:
                betfair_aus_str = part.replace("Betfair Aus:", "").strip()
            elif "Betfair Oman:" in part:
                betfair_oman_str = part.replace("Betfair Oman:", "").strip()
            elif "Poly Aus:" in part:
                poly_aus_str = part.replace("Poly Aus:", "").strip()
            elif "Poly Oman:" in part:
                poly_oman_str = part.replace("Poly Oman:", "").strip()
        
        # Convert to probabilities (keep them separate for comparison)
        betfair_aus_pct = parse_betfair_odds(betfair_aus_str)
        betfair_oman_pct = parse_betfair_odds(betfair_oman_str)
        poly_aus_pct = parse_polymarket_odds(poly_aus_str)
        poly_oman_pct = parse_polymarket_odds(poly_oman_str)
        
        return {
            "Time": time_str,
            "Betfair Australia %": betfair_aus_pct,
            "Betfair Oman %": betfair_oman_pct,
            "Polymarket Australia %": poly_aus_pct,
            "Polymarket Oman %": poly_oman_pct,
        }
        
    except Exception as e:
        logger.debug(f"Error parsing line: {e}")
        return None


def read_and_convert(input_file: Path, output_file: Path) -> int:
    """Read live_odds.txt and convert to Excel. Returns number of rows processed."""
    if not input_file.exists():
        logger.warning(f"Input file not found: {input_file}")
        return 0
    
    logger.info(f"Reading {input_file}...")
    
    rows = []
    with open(input_file, 'r') as f:
        for line in f:
            data = parse_line(line)
            if data:
                rows.append(data)
    
    if not rows:
        logger.warning("No valid data found in input file")
        return 0
    
    # Create DataFrame
    df = pd.DataFrame(rows)
    
    # Write to Excel
    logger.info(f"Writing {len(rows)} rows to {output_file}...")
    df.to_excel(output_file, index=False, sheet_name='Live Odds')
    
    logger.info(f"✓ Excel file updated: {output_file} ({len(rows)} rows)")
    return len(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Convert live_odds.txt to Excel with probabilities"
    )
    parser.add_argument(
        "--input",
        default="data/live_odds.txt",
        help="Input .txt file (default: data/live_odds.txt)",
    )
    parser.add_argument(
        "--output",
        default="data/live_odds.xlsx",
        help="Output Excel file (default: data/live_odds.xlsx)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Update interval in seconds (default: 30.0)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (don't loop)",
    )
    args = parser.parse_args()
    
    input_file = Path(args.input)
    output_file = Path(args.output)
    
    # Create output directory if needed
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    logger.info("=" * 60)
    logger.info("Excel Converter - Live Odds to Excel")
    logger.info("=" * 60)
    logger.info(f"Input file: {input_file}")
    logger.info(f"Output file: {output_file}")
    logger.info(f"Update interval: {args.interval}s")
    logger.info("=" * 60)
    
    if args.once:
        # Run once and exit
        read_and_convert(input_file, output_file)
        return
    
    # Continuous loop
    logger.info("Starting continuous conversion (press Ctrl+C to stop)...")
    try:
        while True:
            rows = read_and_convert(input_file, output_file)
            if rows > 0:
                logger.info(f"Next update in {args.interval}s...")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("\nStopped by user")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)


if __name__ == "__main__":
    main()
