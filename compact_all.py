#!/usr/bin/env python3
"""Recompacta all.ddb con compresión moderna DuckDB (storage latest)."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import duckdb

from duckdb_store import DUCKDB_CONFIG, TABLE


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Recompacta all.ddb con compresión")
    p.add_argument("--input", default="../all.ddb", help="all.ddb origen")
    p.add_argument("--output", default="../all.ddb.new", help="salida compactada")
    p.add_argument("--replace", action="store_true", help="reemplaza input al terminar")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    base = Path(__file__).resolve().parent
    src = Path(args.input)
    dst = Path(args.output)
    if not src.is_absolute():
        src = base / src
    if not dst.is_absolute():
        dst = base / dst

    if not src.is_file():
        raise SystemExit(f"No existe {src}")
    if dst.exists():
        dst.unlink()

    t0 = time.time()
    print(f"Compactando {src} → {dst} …")
    con = duckdb.connect(str(dst), config=DUCKDB_CONFIG)
    try:
        con.execute(
            f"""
            CREATE TABLE {TABLE} (
                tagid INTEGER NOT NULL,
                fecha TIMESTAMP NOT NULL,
                valor DOUBLE NOT NULL,
                PRIMARY KEY (tagid, fecha)
            )
            """
        )
        con.execute(f"ATTACH '{src}' AS s (READ_ONLY)")
        stats = con.execute(f"SELECT count(*), count(DISTINCT tagid) FROM s.{TABLE}").fetchone()
        print(f"  origen: {stats[0]:,} filas, {stats[1]:,} tags")
        con.execute(f"INSERT INTO {TABLE} SELECT tagid, fecha, valor FROM s.{TABLE}")
        con.execute("CHECKPOINT")
        out = con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
    finally:
        con.close()

    gb = dst.stat().st_size / (1024**3)
    src_gb = src.stat().st_size / (1024**3)
    print(f"OK  {out:,} filas")
    print(f"    {src_gb:.1f} GB → {gb:.1f} GB  ({100 * gb / src_gb:.0f}%)")
    print(f"    tiempo: {time.time() - t0:.0f}s")

    if args.replace:
        bak = src.with_suffix(".ddb.bak")
        if bak.exists():
            bak.unlink()
        os.rename(src, bak)
        os.rename(dst, src)
        print(f"    reemplazado; backup en {bak}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
