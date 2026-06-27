#!/usr/bin/env python3
"""Copia tablas SQLite → DuckDB (.ddb)."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import duckdb


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Copia SQLite a DuckDB serializado (.ddb)")
    p.add_argument("--input", required=True, help="Archivo .sqlite origen")
    p.add_argument("--output", required=True, help="Archivo .ddb destino")
    return p.parse_args()


def list_tables(sqlite_path: Path) -> list[str]:
    con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        return [str(r[0]) for r in rows]
    finally:
        con.close()


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def copy_sqlite_to_duckdb(sqlite_path: Path, duck_path: Path) -> dict[str, int]:
    tables = list_tables(sqlite_path)
    if not tables:
        raise SystemExit(f"Sin tablas en {sqlite_path}")

    duck_path.parent.mkdir(parents=True, exist_ok=True)
    if duck_path.exists():
        duck_path.unlink()

    src = str(sqlite_path.resolve())
    counts: dict[str, int] = {}

    con = duckdb.connect(str(duck_path))
    try:
        con.execute("INSTALL sqlite;")
        con.execute("LOAD sqlite;")
        for table in tables:
            qt = _quote_ident(table)
            con.execute(
                f"CREATE TABLE {qt} AS SELECT * FROM sqlite_scan(?, ?)",
                [src, table],
            )
            counts[table] = int(con.execute(f"SELECT count(*) FROM {qt}").fetchone()[0])
    finally:
        con.close()

    return counts


def main() -> None:
    args = parse_args()
    inp = Path(args.input)
    out = Path(args.output)

    if not inp.is_file():
        raise SystemExit(f"No existe: {inp}")

    counts = copy_sqlite_to_duckdb(inp, out)
    size_kb = out.stat().st_size / 1024

    print(f"OK  {inp}  →  {out}")
    for table, n in counts.items():
        print(f"    {table}: {n:,} filas")
    print(f"    tamaño: {size_kb:.1f} KB")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
