#!/usr/bin/env python3
"""Reconstruye tag_summary en all.ddb (detener collector antes)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MY2SQLITE = Path(__file__).resolve().parent
if str(MY2SQLITE) not in sys.path:
    sys.path.insert(0, str(MY2SQLITE))

from duckdb_store import connect, rebuild_tag_summary  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="Reconstruye tag_summary desde medidas_15min")
    p.add_argument(
        "--duckdb",
        default=str(MY2SQLITE.parent / "all.ddb"),
        help="Ruta a all.ddb",
    )
    args = p.parse_args()
    path = Path(args.duckdb)
    con = connect(path)
    try:
        n = rebuild_tag_summary(con)
        print(f"[ok] tag_summary: {n} tags en {path}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
