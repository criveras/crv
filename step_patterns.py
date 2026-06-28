#!/usr/bin/env python3
"""Patron horario LL/HH aprendido para graficos step.

Calcula una banda por hora separando:
- lunes a viernes normal
- sabado/domingo
- feriados Chile que caen lunes-viernes

El patron se guarda en output/patterns/*.json para usarlo desde /api/chart sin recalcular en el navegador.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

TZ_NAME = "America/Santiago"
DAY_TYPES = ("weekday", "weekend", "holiday")


def _tz(cfg: dict | None = None) -> ZoneInfo:
    return ZoneInfo((cfg or {}).get("timezone") or TZ_NAME)


def _safe_name(point: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", point).strip("_") or "point"


def pattern_dir(cfg: dict | None = None) -> Path:
    base = Path((cfg or {}).get("model_dir", "output"))
    return base / "patterns"


def pattern_path(point: str, cfg: dict | None = None) -> Path:
    return pattern_dir(cfg) / f"{_safe_name(point)}.step_pattern.json"


def _easter_date(year: int) -> datetime:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    h = (19 * a + b - d - f + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime(year, month, day)


def is_chile_holiday(dt: datetime) -> bool:
    fixed = {
        "01-01", "05-01", "05-21", "06-20", "06-29", "07-16", "08-15",
        "09-18", "09-19", "10-12", "10-31", "11-01", "12-08", "12-25",
    }
    md = dt.strftime("%m-%d")
    if md in fixed:
        return True
    easter = _easter_date(dt.year)
    return dt.date() in {(easter - timedelta(days=2)).date(), (easter - timedelta(days=1)).date()}


def day_type(ts: pd.Timestamp | datetime, cfg: dict | None = None) -> str:
    tz = _tz(cfg)
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        local = t.tz_localize(tz)
    else:
        local = t.tz_convert(tz)
    py = local.to_pydatetime()
    if is_chile_holiday(py) and py.weekday() < 5:
        return "holiday"
    if py.weekday() >= 5:
        return "weekend"
    return "weekday"


def _hour(ts: pd.Timestamp | datetime, cfg: dict | None = None) -> int:
    tz = _tz(cfg)
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return int(t.hour)
    return int(t.tz_convert(tz).hour)


def _stats(values: list[float], min_samples: int = 3) -> dict[str, float] | None:
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if len(clean) < min_samples:
        return None
    s = pd.Series(clean, dtype="float64")
    p10 = float(s.quantile(0.10))
    p50 = float(s.quantile(0.50))
    p90 = float(s.quantile(0.90))
    sigma = (p90 - p10) / 2.563
    if not math.isfinite(sigma) or sigma <= 0:
        sigma = max(abs(p50) * 0.01, 0.01)
    return {
        "p10": round(p10, 6),
        "p50": round(p50, 6),
        "p90": round(p90, 6),
        "sigma": round(sigma, 6),
        "ll": round(p50 - 3 * sigma, 6),
        "hh": round(p50 + 3 * sigma, 6),
        "n": len(clean),
    }


def learn_step_pattern(df: pd.DataFrame, point: str, cfg: dict | None = None) -> dict[str, Any]:
    cfg = cfg or {}
    min_samples = int(cfg.get("step_pattern_min_samples", 3))
    buckets: dict[str, dict[int, list[float]]] = {k: {h: [] for h in range(24)} for k in DAY_TYPES}
    source = df.dropna(subset=["time_local", "value"]).copy()
    for row in source.itertuples():
        dtype = day_type(row.time_local, cfg)
        hour = _hour(row.time_local, cfg)
        val = float(row.value)
        if math.isfinite(val):
            buckets[dtype][hour].append(val)
    pattern: dict[str, Any] = {k: {} for k in DAY_TYPES}
    for dtype in DAY_TYPES:
        for hour in range(24):
            st = _stats(buckets[dtype][hour], min_samples=min_samples)
            if st:
                pattern[dtype][f"{hour:02d}"] = st
    meta = {
        "point": point,
        "timezone": cfg.get("timezone") or TZ_NAME,
        "generated_at": datetime.now(_tz(cfg)).isoformat(timespec="seconds"),
        "source_rows": int(len(source)),
        "min_samples": min_samples,
        "method": "hourly p10/p50/p90 sigma3",
    }
    return {"meta": meta, "pattern": pattern}


def save_step_pattern(df: pd.DataFrame, point: str, cfg: dict | None = None) -> Path:
    data = learn_step_pattern(df, point, cfg)
    path = pattern_path(point, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_step_pattern(point: str, cfg: dict | None = None) -> dict[str, Any] | None:
    path = pattern_path(point, cfg)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _value_for(pattern: dict[str, Any], dtype: str, hour: int) -> dict[str, Any] | None:
    p = pattern.get("pattern", {})
    key = f"{hour:02d}"
    if dtype == "weekday":
        return (p.get("weekday") or {}).get(key)
    # feriados usan patron weekend si no hay suficiente muestra de feriado
    return (p.get(dtype) or {}).get(key) or (p.get("weekend") or {}).get(key) or (p.get("weekday") or {}).get(key)


def _ms(ts: pd.Timestamp) -> int:
    return int(ts.timestamp() * 1000)


def build_step_overlay(point: str, df: pd.DataFrame, cfg: dict | None = None) -> dict[str, Any] | None:
    cfg = cfg or {}
    pat = load_step_pattern(point, cfg)
    if pat is None:
        # primer uso: genera cache con el rango disponible para no dejar la pantalla sin banda
        try:
            save_step_pattern(df, point, cfg)
            pat = load_step_pattern(point, cfg)
        except Exception:
            pat = None
    if pat is None or df.empty or "time_local" not in df.columns:
        return None
    tz = _tz(cfg)
    start = pd.Timestamp(df["time_local"].min())
    end = pd.Timestamp(df["time_local"].max())
    if start.tzinfo is None:
        start = start.tz_localize(tz)
    else:
        start = start.tz_convert(tz)
    if end.tzinfo is None:
        end = end.tz_localize(tz)
    else:
        end = end.tz_convert(tz)
    cur = start.floor("h")
    end = end.floor("h") + pd.Timedelta(hours=1)
    rows = {"weekday": [], "weekend": [], "holiday": []}
    prev_key: str | None = None
    while cur <= end:
        dtype = day_type(cur, cfg)
        st = _value_for(pat, dtype, int(cur.hour))
        if st:
            if prev_key and prev_key != dtype and rows[prev_key]:
                rows[prev_key].append([_ms(cur), None, None])
            rows[dtype].append([_ms(cur), round(float(st["ll"]), 3), round(float(st["hh"]), 3)])
            prev_key = dtype
        cur += pd.Timedelta(hours=1)
    return {"meta": pat.get("meta", {}), "series": rows}
