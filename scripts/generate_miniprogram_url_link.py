#!/usr/bin/env python3
"""Generate a WeChat URL Link using the configured mini-program credentials."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.miniprogram_url_link_service import generate_url_link


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="pages/halloween/index")
    parser.add_argument("--query", default="")
    parser.add_argument("--expire-days", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(generate_url_link(vars(args)), ensure_ascii=False))


if __name__ == "__main__":
    main()
