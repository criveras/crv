#!/usr/bin/env python3
"""Cartas de control Six Sigma — bandas ±σ y reglas de patrón (Nelson/Western Electric)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from features import dow_key, minute_offset

SIGMA_COLS = ("cl", "s1_lo", "s1_hi", "s2_lo", "s2_hi", "s3_lo", "s3_hi")


def _slot_stats(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {}
    if len(vals) < 2:
        m = float(vals[0])
        return {"cl": m, "sigma": 0.0}
    arr = np.asarray(vals, dtype=float)
    mu = float(arr.mean())
    sigma = float(arr.std(ddof=1))
    if sigma <= 0:
        sigma = float((arr.max() - arr.min()) / 4) if arr.max() != arr.min() else max(abs(mu) * 0.01, 0.01)
    return {"cl": mu, "sigma": sigma}


def _attach_sigma(row: pd.Series, stats: dict[tuple[str, int], dict[str, float]], stats_all: dict[int, dict[str, float]], use_dow: bool) -> dict[str, float]:
    band = stats.get((row["_dow"], row["_off"]), {}) if use_dow else {}
    if not band:
        band = stats_all.get(row["_off"], {})
    if not band:
        return {}
    cl = band["cl"]
    s = band["sigma"]
    return {
        "cl": cl,
        "s1_lo": cl - s,
        "s1_hi": cl + s,
        "s2_lo": cl - 2 * s,
        "s2_hi": cl + 2 * s,
        "s3_lo": cl - 3 * s,
        "s3_hi": cl + 3 * s,
    }


def apply_sigma_lh_limits(df: pd.DataFrame, sigma_n: int) -> pd.DataFrame:
    """Sustituye L/H (banda verde) por ±Nσ respecto a la línea central."""
    if sigma_n not in (1, 2, 3):
        return df
    lo_col = f"s{sigma_n}_lo"
    hi_col = f"s{sigma_n}_hi"
    if lo_col not in df.columns or hi_col not in df.columns:
        return df
    out = df.copy()
    mask = out[lo_col].notna() & out[hi_col].notna()
    if "l" in out.columns:
        out["l"] = out["l"].astype(float)
    if "h" in out.columns:
        out["h"] = out["h"].astype(float)
    out.loc[mask, "l"] = out.loc[mask, lo_col].astype(float)
    out.loc[mask, "h"] = out.loc[mask, hi_col].astype(float)
    return out


def build_sigma_bands(df: pd.DataFrame, use_dow: bool = True, min_samples: int = 3) -> pd.DataFrame:
    """Línea central y bandas ±1σ/±2σ/±3σ por hora y día (wd/sat/sun)."""
    if df.empty:
        return df.copy()

    out = df.copy()
    out["_off"] = out["time_utc"].apply(minute_offset)
    out["_dow"] = out["time_utc"].apply(dow_key)

    by_dow: dict[tuple[str, int], list[float]] = {}
    by_all: dict[int, list[float]] = {}
    for _, row in out.iterrows():
        off = row["_off"]
        by_all.setdefault(off, []).append(row["value"])
        if use_dow:
            by_dow.setdefault((row["_dow"], off), []).append(row["value"])

    stats_dow = {k: _slot_stats(v) for k, v in by_dow.items() if len(v) >= min_samples}
    stats_all = {off: _slot_stats(v) for off, v in by_all.items() if v}

    for col in SIGMA_COLS:
        out[col] = np.nan

    for idx, row in out.iterrows():
        sig = _attach_sigma(row, stats_dow, stats_all, use_dow)
        for col, val in sig.items():
            out.at[idx, col] = val

    return out.drop(columns=["_off", "_dow"])


def _ts_ms(ts: pd.Timestamp) -> int:
    return int(ts.timestamp() * 1000)


def detect_patterns(df: pd.DataFrame, cfg: dict | None = None) -> dict[str, Any]:
    """
    Detecta patrones de carta de control:
    - stuck: puntos pegados (mismo valor)
    - trend_up / trend_down: racha ascendente/descendente
    - ooc_high / ooc_low: fuera de ±3σ
    - run_above / run_below: 8 puntos consecutivos sobre/bajo CL
    """
    cfg = cfg or {}
    eps = float(cfg.get("sixsigma_stuck_eps", 0.02))
    min_stuck = int(cfg.get("sixsigma_stuck_min", 5))
    trend_len = int(cfg.get("sixsigma_trend_len", 6))
    run_len = int(cfg.get("sixsigma_run_len", 8))

    work = df.dropna(subset=["value"]).reset_index(drop=True)
    if work.empty:
        return {"events": [], "summary": {}}

    y = work["value"].astype(float).values
    n = len(y)
    events: list[dict[str, Any]] = []
    seen_stuck: set[int] = set()

    def _point(i: int, ptype: str, label: str) -> dict[str, Any]:
        row = work.iloc[i]
        return {
            "type": ptype,
            "label": label,
            "x": _ts_ms(row["time_local"]),
            "y": round(float(row["value"]), 3),
            "time": str(row["time_local"]),
        }

    # Puntos pegados
    run = 1
    for i in range(1, n):
        if abs(y[i] - y[i - 1]) <= eps:
            run += 1
            if run >= min_stuck and i not in seen_stuck:
                seen_stuck.add(i)
                events.append(_point(i, "stuck", f"Pegado ({run} pts)"))
        else:
            run = 1

    # Tendencia ascendente / descendente
    up = 1
    down = 1
    for i in range(1, n):
        if y[i] > y[i - 1]:
            up += 1
            down = 1
            if up == trend_len:
                events.append(_point(i, "trend_up", f"Ascendente ({trend_len} pts)"))
        elif y[i] < y[i - 1]:
            down += 1
            up = 1
            if down == trend_len:
                events.append(_point(i, "trend_down", f"Descendente ({trend_len} pts)"))
        else:
            up = down = 1

    # Fuera de ±3σ y carreras sobre/bajo CL
    if "s3_hi" in work.columns and "cl" in work.columns:
        above = 0
        below = 0
        for i in range(n):
            row = work.iloc[i]
            hi = row.get("s3_hi")
            lo = row.get("s3_lo")
            cl = row.get("cl")
            if pd.notna(hi) and y[i] > hi:
                events.append(_point(i, "ooc_high", "Fuera +3σ"))
            if pd.notna(lo) and y[i] < lo:
                events.append(_point(i, "ooc_low", "Fuera −3σ"))
            if pd.notna(cl):
                if y[i] > cl:
                    above += 1
                    below = 0
                    if above == run_len:
                        events.append(_point(i, "run_above", f"Sobre CL ({run_len} pts)"))
                elif y[i] < cl:
                    below += 1
                    above = 0
                    if below == run_len:
                        events.append(_point(i, "run_below", f"Bajo CL ({run_len} pts)"))
                else:
                    above = below = 0

    by_type: dict[str, int] = {}
    for ev in events:
        by_type[ev["type"]] = by_type.get(ev["type"], 0) + 1

    return {"events": events, "summary": by_type}


def pattern_markers(patterns: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Agrupa eventos para el gráfico."""
    out: dict[str, list[dict[str, Any]]] = {}
    for ev in patterns.get("events", []):
        out.setdefault(ev["type"], []).append({"x": ev["x"], "y": ev["y"], "label": ev["label"]})
    return out
