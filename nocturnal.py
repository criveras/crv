#!/usr/bin/env python3
"""Análisis de mínimos y máximos nocturnos del caudal."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def night_mask(
    df: pd.DataFrame,
    start_hour: int = 22,
    end_hour: int = 6,
) -> pd.Series:
    """True si la hora local cae en ventana nocturna [start_hour, end_hour)."""
    h = df["time_local"].dt.hour
    if start_hour > end_hour:
        return (h >= start_hour) | (h < end_hour)
    return (h >= start_hour) & (h < end_hour)


def daily_nocturnal_stats(
    df: pd.DataFrame,
    start_hour: int = 22,
    end_hour: int = 6,
) -> pd.DataFrame:
    """Min/max/mean nocturno por día local."""
    work = df.copy()
    work["date_local"] = work["time_local"].dt.date
    mask = night_mask(work, start_hour, end_hour)
    night = work.loc[mask]

    if night.empty:
        return pd.DataFrame(columns=["date_local", "night_min", "night_max", "night_mean", "night_count"])

    return (
        night.groupby("date_local")["value"]
        .agg(night_min="min", night_max="max", night_mean="mean", night_count="count")
        .reset_index()
    )


def global_nocturnal_summary(stats: pd.DataFrame) -> dict[str, Any]:
    """Resumen histórico de extremos nocturnos."""
    if stats.empty:
        return {}
    return {
        "n_nights": int(len(stats)),
        "min_nocturno_global": round(float(stats["night_min"].min()), 3),
        "max_nocturno_global": round(float(stats["night_max"].max()), 3),
        "mean_night_min": round(float(stats["night_min"].mean()), 3),
        "mean_night_max": round(float(stats["night_max"].mean()), 3),
        "p10_night_min": round(float(np.percentile(stats["night_min"], 10)), 3),
        "p90_night_max": round(float(np.percentile(stats["night_max"], 90)), 3),
    }


def hourly_nocturnal_profile(
    df: pd.DataFrame,
    start_hour: int = 22,
    end_hour: int = 6,
) -> pd.DataFrame:
    """Perfil hora a hora durante la noche."""
    work = df.copy()
    mask = night_mask(work, start_hour, end_hour)
    night = work.loc[mask].copy()
    if night.empty:
        return pd.DataFrame()
    night["hour_local"] = night["time_local"].dt.hour
    return (
        night.groupby("hour_local")["value"]
        .agg(min="min", max="max", mean="mean", count="count")
        .reset_index()
        .sort_values("hour_local")
    )


def detect_nocturnal_anomalies(
    stats: pd.DataFrame,
    low_pct: float = 10,
    high_pct: float = 90,
) -> pd.DataFrame:
    """Noches con min/max fuera de percentiles históricos."""
    if stats.empty or len(stats) < 5:
        return pd.DataFrame()

    low_thr = np.percentile(stats["night_min"], low_pct)
    high_thr = np.percentile(stats["night_max"], high_pct)

    flags = stats.copy()
    flags["anomaly"] = ""
    flags.loc[flags["night_min"] < low_thr, "anomaly"] = "min_bajo"
    flags.loc[flags["night_max"] > high_thr, "anomaly"] = "max_alto"
    flags.loc[
        (flags["night_min"] < low_thr) & (flags["night_max"] > high_thr),
        "anomaly",
    ] = "min_bajo_y_max_alto"
    return flags[flags["anomaly"] != ""]
