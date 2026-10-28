"""Drain coalesced market updates, separately from interactive upload workers."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.formula_calculation_service import drain_market_once
if __name__ == '__main__':
    print(json.dumps(drain_market_once(), ensure_ascii=False))
