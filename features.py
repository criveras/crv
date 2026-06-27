#!/usr/bin/env python3
"""Bandas percentiles y features para detección pre-rotura."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    i = (len(s) - 1) * p
    lo, hi = int(i), min(int(i) + 1, len(s) - 1)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def minute_offset(ts: pd.Timestamp) -> int:
    """Segundos desde medianoche UTC truncado al minuto."""
    t = ts.to_pydatetime()
    if t.tzinfo:
        t = t.astimezone(datetime.now().astimezone().tzinfo).replace(tzinfo=None)
    return ((t.hour * 60 + t.minute) * 60) % 86400


def dow_key(ts: pd.Timestamp) -> str:
    wd = ts.weekday()
    if wd == 5:
        return "sat"
    if wd == 6:
        return "sun"
    return "wd"


def band_percentiles(
    vals: list[float],
    warn_low: float = 0.10,
    warn_high: float = 0.90,
    alarm_low: float = 0.02,
    alarm_high: float = 0.98,
) -> dict[str, float]:
    return {
        "p05": percentile(vals, 0.05),
        "p20": percentile(vals, 0.20),
        "p50": percentile(vals, 0.50),
        "p80": percentile(vals, 0.80),
        "p95": percentile(vals, 0.95),
        "l": percentile(vals, warn_low),
        "h": percentile(vals, warn_high),
        "ll": percentile(vals, alarm_low),
        "hh": percentile(vals, alarm_high),
    }


def build_trend_bands(
    df: pd.DataFrame,
    use_dow: bool = True,
    warn_low: float = 0.10,
    warn_high: float = 0.90,
    alarm_low: float = 0.02,
    alarm_high: float = 0.98,
) -> pd.DataFrame:
    """Percentiles diurnos por franja horaria y día (lun–vie / sáb / dom)."""
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

    pc_dow = {
        k: band_percentiles(v, warn_low, warn_high, alarm_low, alarm_high)
        for k, v in by_dow.items()
        if len(v) >= 3
    }
    pc_all = {
        off: band_percentiles(v, warn_low, warn_high, alarm_low, alarm_high)
        for off, v in by_all.items()
        if v
    }

    band_cols = ("p05", "p20", "p50", "p80", "p95", "l", "h", "ll", "hh")
    for col in band_cols:
        out[col] = np.nan

    for idx, row in out.iterrows():
        band = pc_dow.get((row["_dow"], row["_off"]), {}) if use_dow else {}
        if not band:
            band = pc_all.get(row["_off"], {})
        for col in band_cols:
            out.at[idx, col] = band.get(col, np.nan)

    return out.drop(columns=["_off", "_dow"])


def rolling_delta(series: pd.Series, steps: int) -> pd.Series:
    return series - series.shift(steps)


def rolling_delta_max(values: np.ndarray, window: int) -> np.ndarray:
    """Máximo |Δy| en ventana móvil de `window` pasos."""
    n = len(values)
    out = np.zeros(n)
    window = max(1, window)
    for i in range(1, n):
        if window == 1:
            out[i] = abs(values[i] - values[i - 1])
            continue
        lo = max(0, i - window + 1)
        deltas = np.abs(np.diff(values[lo : i + 1]))
        out[i] = deltas.max() if len(deltas) else 0.0
    return out


def label_rupture_events(
    df: pd.DataFrame,
    delta_umbral: float = 3.0,
    win_steps: int = 1,
) -> pd.Series:
    """Marca instantes de posible rotura (subida brusca sobre banda alta)."""
    y = df["value"].values
    delta_up = np.zeros(len(df))
    for i in range(1, len(df)):
        dy = y[i] - y[i - 1]
        delta_up[i] = dy if dy > 0 else 0.0

    delta_peak = rolling_delta_max(delta_up, win_steps)
    p80 = df["p80"].values if "p80" in df.columns else np.full(len(df), np.nan)

    labels = np.zeros(len(df), dtype=bool)
    for i in range(len(df)):
        sobre_banda = (
            (not np.isnan(p80[i]) and y[i] > p80[i])
            or (not np.isnan(df["p95"].iloc[i]) and y[i] > df["p95"].iloc[i])
        )
        if delta_peak[i] > delta_umbral and sobre_banda:
            labels[i] = True
    return pd.Series(labels, index=df.index, name="rupture")


def label_pre_rupture(df: pd.DataFrame, lookahead_steps: int) -> pd.Series:
    """1 si hay rotura en los próximos `lookahead_steps` intervalos."""
    rupture = df["rupture"].values.astype(bool)
    n = len(rupture)
    pre = np.zeros(n, dtype=bool)
    for i in range(n):
        end = min(n, i + lookahead_steps + 1)
        pre[i] = rupture[i + 1 : end].any()
    return pd.Series(pre, index=df.index, name="pre_rupture")


def build_features(
    df: pd.DataFrame,
    night_mask: pd.Series,
    nocturnal_stats: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Feature matrix para XGBoost."""
    out = df.copy()
    tl = out["time_local"]

    out["hour"] = tl.dt.hour + tl.dt.minute / 60.0
    out["dow"] = tl.dt.dayofweek
    out["is_night"] = night_mask.astype(int)

    for lag in (1, 2, 4, 8):
        out[f"lag_{lag}"] = out["value"].shift(lag)

    for steps in (1, 2, 4):
        out[f"delta_{steps}"] = rolling_delta(out["value"], steps)

    out["delta_up_4"] = out["delta_4"].clip(lower=0)
    out["delta_down_4"] = (-out["delta_4"]).clip(lower=0)
    out["delta_max_4"] = rolling_delta_max(out["value"].values, 4)

    for col in ("p05", "p20", "p50", "p80", "p95"):
        if col in out.columns:
            out[f"dev_{col}"] = out["value"] - out[col]
            out[f"ratio_{col}"] = out["value"] / out[col].replace(0, np.nan)

    out["roll_mean_8"] = out["value"].rolling(8, min_periods=1).mean()
    out["roll_std_8"] = out["value"].rolling(8, min_periods=1).std().fillna(0)
    out["roll_min_8"] = out["value"].rolling(8, min_periods=1).min()
    out["roll_max_8"] = out["value"].rolling(8, min_periods=1).max()

    if nocturnal_stats is not None and not nocturnal_stats.empty:
        out = out.merge(nocturnal_stats, on="date_local", how="left")
        out["vs_night_min"] = out["value"] - out["night_min"]
        out["vs_night_max"] = out["value"] - out["night_max"]
        out["night_range"] = out["night_max"] - out["night_min"]

    return out


FEATURE_COLS = [
    "hour",
    "dow",
    "is_night",
    "value",
    "lag_1",
    "lag_2",
    "lag_4",
    "lag_8",
    "delta_1",
    "delta_2",
    "delta_4",
    "delta_up_4",
    "delta_down_4",
    "delta_max_4",
    "dev_p50",
    "dev_p80",
    "dev_p95",
    "ratio_p80",
    "roll_mean_8",
    "roll_std_8",
    "roll_min_8",
    "roll_max_8",
    "vs_night_min",
    "vs_night_max",
    "night_range",
    "night_min",
    "night_max",
    "night_mean",
]


def feature_matrix(df: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    cols = [c for c in FEATURE_COLS if c in df.columns]
    X = df[cols].replace([np.inf, -np.inf], np.nan).fillna(0).values
    return X, cols
