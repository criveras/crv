#!/usr/bin/env python3
"""Almacén DuckDB unificado para medianas 15 min (all.ddb)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

import duckdb

TABLE = "medidas_15min"
SUMMARY_TABLE = "tag_summary"
DUCKDB_CONFIG = {"storage_compatibility_version": "latest"}
SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    tagid INTEGER NOT NULL,
    fecha TIMESTAMP NOT NULL,
    valor DOUBLE NOT NULL,
    PRIMARY KEY (tagid, fecha)
)
"""
SUMMARY_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {SUMMARY_TABLE} (
    tagid INTEGER PRIMARY KEY,
    rows INTEGER NOT NULL,
    first_fecha TIMESTAMP,
    last_fecha TIMESTAMP,
    last_valor DOUBLE
)
"""
SUMMARY_UPSERT_SQL = f"""
INSERT INTO {SUMMARY_TABLE} (tagid, rows, first_fecha, last_fecha, last_valor)
VALUES (?, 1, ?::TIMESTAMP, ?::TIMESTAMP, ?)
ON CONFLICT (tagid) DO UPDATE SET
    rows = {SUMMARY_TABLE}.rows + CASE
        WHEN excluded.last_fecha > {SUMMARY_TABLE}.last_fecha THEN 1
        ELSE 0
    END,
    first_fecha = LEAST(
        COALESCE({SUMMARY_TABLE}.first_fecha, excluded.first_fecha),
        excluded.first_fecha
    ),
    last_fecha = GREATEST(
        COALESCE({SUMMARY_TABLE}.last_fecha, excluded.last_fecha),
        excluded.last_fecha
    ),
    last_valor = CASE
        WHEN excluded.last_fecha >= COALESCE({SUMMARY_TABLE}.last_fecha, excluded.last_fecha)
        THEN excluded.last_valor
        ELSE {SUMMARY_TABLE}.last_valor
    END
"""


def connect(duck_path: Path) -> duckdb.DuckDBPyConnection:
    duck_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(duck_path), config=DUCKDB_CONFIG)
    ensure_schema(con)
    return con


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA_SQL)
    con.execute(SUMMARY_SCHEMA_SQL)


def fecha_str(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def update_tag_summary(
    con: duckdb.DuckDBPyConnection,
    window_fecha: datetime,
    rows: Iterable[tuple[int, float]],
) -> None:
    ts = fecha_str(window_fecha)
    batch = [(int(tagid), ts, float(valor)) for tagid, valor in rows]
    if not batch:
        return
    con.executemany(SUMMARY_UPSERT_SQL, batch)


def rebuild_tag_summary(con: duckdb.DuckDBPyConnection) -> int:
    """Reconstruye tag_summary desde medidas_15min (ejecutar con collector detenido)."""
    ensure_schema(con)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {SUMMARY_TABLE} AS
        SELECT
            tagid,
            count(*)::INTEGER AS rows,
            min(fecha) AS first_fecha,
            max(fecha) AS last_fecha,
            arg_max(valor, fecha) AS last_valor
        FROM {TABLE}
        GROUP BY tagid
        """
    )
    row = con.execute(f"SELECT count(*) FROM {SUMMARY_TABLE}").fetchone()
    return int(row[0]) if row else 0


def save_medians(
    con: duckdb.DuckDBPyConnection,
    window_fecha: datetime,
    rows: Iterable[tuple[int, float]],
) -> int:
    """Inserta o reemplaza medianas para una ventana. Retorna filas escritas."""
    ts = fecha_str(window_fecha)
    batch = [(int(tagid), ts, float(valor)) for tagid, valor in rows]
    if not batch:
        return 0
    con.executemany(
        f"""
        INSERT INTO {TABLE} (tagid, fecha, valor) VALUES (?, ?::TIMESTAMP, ?)
        ON CONFLICT (tagid, fecha) DO UPDATE SET valor = excluded.valor
        """,
        batch,
    )
    update_tag_summary(con, window_fecha, rows)
    return len(batch)


def tag_stats(con: duckdb.DuckDBPyConnection, tagid: int) -> dict | None:
    row = con.execute(
        f"""
        SELECT rows, first_fecha, last_fecha, last_valor
        FROM {SUMMARY_TABLE}
        WHERE tagid = ?
        """,
        [tagid],
    ).fetchone()
    if row:
        last_fecha = row[2]
        first_fecha = row[1]
        return {
            "tagid": tagid,
            "rows": int(row[0]),
            "first_fecha": first_fecha.isoformat(timespec="minutes")
            if hasattr(first_fecha, "isoformat")
            else str(first_fecha),
            "last_fecha": last_fecha.isoformat(timespec="minutes")
            if hasattr(last_fecha, "isoformat")
            else str(last_fecha),
            "last_valor": float(row[3]) if row[3] is not None else None,
        }

    row = con.execute(
        f"""
        SELECT
            count(*) AS rows,
            min(fecha) AS first_fecha,
            max(fecha) AS last_fecha,
            arg_max(valor, fecha) AS last_valor
        FROM {TABLE}
        WHERE tagid = ?
        """,
        [tagid],
    ).fetchone()
    if not row or row[0] == 0:
        return None
    last_fecha = row[2]
    first_fecha = row[1]
    return {
        "tagid": tagid,
        "rows": int(row[0]),
        "first_fecha": first_fecha.isoformat(timespec="minutes")
        if hasattr(first_fecha, "isoformat")
        else str(first_fecha),
        "last_fecha": last_fecha.isoformat(timespec="minutes")
        if hasattr(last_fecha, "isoformat")
        else str(last_fecha),
        "last_valor": float(row[3]) if row[3] is not None else None,
    }
