#!/usr/bin/env python3
"""Cliente RT3 — extracción de series vía rt3-apirun (mismo contrato que areatrend)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

RT3_HOST = os.environ.get("RT3_API_HOST", "http://rt3-d2:8090")
API_BASE = os.environ.get("RT3_API_BASE", f"{RT3_HOST}/read")
TIMEOUT = int(os.environ.get("RT3_API_TIMEOUT", "120"))


def parse_ts(ts: str) -> datetime:
    """YYYYMMDDHHMMSS → datetime UTC."""
    return datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def fetch_read_raw(point: str, fini: str, ffin: str = "*", ma: int = 15) -> dict[str, float]:
    r = requests.get(
        API_BASE,
        params={"point": point, "fini": fini, "ffin": ffin, "ma": ma},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    raw = r.json()
    if isinstance(raw, dict) and raw.get("error"):
        raise ValueError(raw["error"])
    return {str(k): float(v) for k, v in raw.items()}


def dict_to_dataframe(data: dict[str, Any], tz: str = "America/Santiago") -> pd.DataFrame:
    rows: list[tuple[datetime, float]] = []
    for k, v in data.items():
        if v is None:
            continue
        try:
            rows.append((parse_ts(str(k)), float(v)))
        except (TypeError, ValueError):
            continue
    if not rows:
        return pd.DataFrame(columns=["time_utc", "value", "time_local"])
    df = pd.DataFrame(rows, columns=["time_utc", "value"])
    df = df.sort_values("time_utc").drop_duplicates("time_utc").reset_index(drop=True)
    df["time_local"] = df["time_utc"].dt.tz_convert(tz)
    return df


def fetch_series(point: str, fini: str, ffin: str = "*", ma: int = 15, tz: str = "America/Santiago") -> pd.DataFrame:
    raw = fetch_read_raw(point, fini, ffin, ma)
    return dict_to_dataframe(raw, tz=tz)
