#!/usr/bin/env python3
"""Migra tag_*.sqlite → un solo all.ddb."""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import duckdb

from duckdb_store import TABLE, connect, ensure_schema

TAG_FILE_RE = re.compile(r"^tag_(\d+)\.sqlite$", re.I)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Consolida SQLite de tags en un all.ddb")
    p.add_argument(
        "--indir",
        default="data",
        help="Directorio con tag_*.sqlite (default: data)",
    )
    p.add_argument(
        "--output",
        default="../all.ddb",
        help="Archivo DuckDB destino (default: ../all.ddb → simula/all.ddb)",
    )
    p.add_argument(
        "--append",
        action="store_true",
        help="No borra all.ddb existente; omite tags ya migrados",
    )
    return p.parse_args()


def list_sqlites(indir: Path) -> list[tuple[int, Path]]:
    out: list[tuple[int, Path]] = []
    for path in sorted(indir.glob("tag_*.sqlite")):
        m = TAG_FILE_RE.match(path.name)
        if m:
            out.append((int(m.group(1)), path))
    return out


def migrated_tagids(con: duckdb.DuckDBPyConnection) -> set[int]:
    rows = con.execute(f"SELECT DISTINCT tagid FROM {TABLE}").fetchall()
    return {int(r[0]) for r in rows}


def import_one(con: duckdb.DuckDBPyConnection, tagid: int, sqlite_path: Path) -> int:
    con.execute("INSTALL sqlite;")
    con.execute("LOAD sqlite;")
    src = str(sqlite_path.resolve())
    table = f"tag_{tagid}"
    con.execute(f"DELETE FROM {TABLE} WHERE tagid = ?", [tagid])
    con.execute(
        f"""
        INSERT INTO {TABLE} (tagid, fecha, valor)
        SELECT {tagid}, CAST(fecha AS TIMESTAMP), valor
        FROM sqlite_scan(?, ?)
        """,
        [src, table],
    )
    return int(con.execute(f"SELECT count(*) FROM {TABLE} WHERE tagid = ?", [tagid]).fetchone()[0])


def main() -> None:
    args = parse_args()
    base = Path(__file__).resolve().parent
    indir = Path(args.indir)
    if not indir.is_absolute():
        indir = base / indir
    output = Path(args.output)
    if not output.is_absolute():
        output = base / output

    if not indir.is_dir():
        raise SystemExit(f"No existe directorio: {indir}")

    files = list_sqlites(indir)
    if not files:
        raise SystemExit(f"Sin tag_*.sqlite en {indir}")

    if output.exists() and not args.append:
        output.unlink()

    con = connect(output)
    try:
        skip: set[int] = set()
        if args.append:
            skip = migrated_tagids(con)
            print(f"Modo append: {len(skip)} tags ya en {output.name}")

        t0 = time.time()
        total_new = 0
        done = 0
        for tagid, path in files:
            if tagid in skip:
                continue
            try:
                n = import_one(con, tagid, path)
                total_new += n
                done += 1
                if done % 100 == 0 or done == len(files) - len(skip):
                    print(f"  [{done}] tag {tagid} +{n:,} filas …")
            except Exception as exc:
                print(f"  ERROR tag {tagid} ({path.name}): {exc}", file=sys.stderr)

        stats = con.execute(
            f"""
            SELECT count(DISTINCT tagid), count(*), min(fecha), max(fecha)
            FROM {TABLE}
            """
        ).fetchone()
    finally:
        con.close()

    elapsed = time.time() - t0
    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"\nOK  {output}")
    print(f"    tags:   {stats[0]:,}")
    print(f"    filas:  {stats[1]:,}  (+{total_new:,} nuevas)")
    print(f"    rango:  {stats[2]} → {stats[3]}")
    print(f"    tamaño: {size_mb:.1f} MB")
    print(f"    tiempo: {elapsed:.0f}s")


if __name__ == "__main__":
    main()
