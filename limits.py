#!/usr/bin/env python3
"""Límites L/LL/H/HH aprendidos del histórico — global, nocturno y diurno."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from anomaly_engine import anomaly_percentiles
from nocturnal import night_mask

BASE_DIR = Path(__file__).resolve().parent


def point_slug(point: str) -> str:
    return point.replace(".", "_").replace("/", "_")


def daily_diurnal_stats(
    df: pd.DataFrame,
    start_hour: int = 22,
    end_hour: int = 6,
) -> pd.DataFrame:
    """Min/max/mean diurno por día local (complemento de la ventana nocturna)."""
    work = df.copy()
    work["date_local"] = work["time_local"].dt.date
    mask = ~night_mask(work, start_hour, end_hour)
    day = work.loc[mask]

    if day.empty:
        return pd.DataFrame(columns=["date_local", "day_min", "day_max", "day_mean", "day_count"])

    return (
        day.groupby("date_local")["value"]
        .agg(day_min="min", day_max="max", day_mean="mean", day_count="count")
        .reset_index()
    )


def _pct(series: pd.Series, p: float, fallback: float) -> float:
    if series.empty or len(series) < 3:
        return fallback
    return float(np.percentile(series.dropna(), p))


def compute_limits(
    df: pd.DataFrame,
    noct_stats: pd.DataFrame,
    diur_stats: pd.DataFrame,
    cfg: dict,
) -> dict[str, Any]:
    """
    Deriva límites desde percentiles históricos.

    - L/H (p10/p90): advertencia temprana — zona verde
    - LL/HH (p02/p98): alarma segura con persistencia
    - bandas por franja horaria y día (lun–vie / sáb / dom)
    """
    pct = anomaly_percentiles(cfg)
    values = df["value"].dropna()

    g_l = round(float(np.percentile(values, pct["l"] * 100)), 3)
    g_h = round(float(np.percentile(values, pct["h"] * 100)), 3)
    g_ll = round(float(np.percentile(values, pct["ll"] * 100)), 3)
    g_hh = round(float(np.percentile(values, pct["hh"] * 100)), 3)

    n_min_ll = round(_pct(noct_stats["night_min"], pct["ll"] * 100, g_ll), 3)
    n_max_hh = round(_pct(noct_stats["night_max"], pct["hh"] * 100, g_hh), 3)
    n_min_l = round(_pct(noct_stats["night_min"], pct["l"] * 100, g_l), 3)
    n_max_h = round(_pct(noct_stats["night_max"], pct["h"] * 100, g_h), 3)
    n_min_obs = round(float(noct_stats["night_min"].min()), 3) if not noct_stats.empty else g_ll
    n_max_obs = round(float(noct_stats["night_max"].max()), 3) if not noct_stats.empty else g_hh

    d_min_ll = round(_pct(diur_stats["day_min"], pct["ll"] * 100, g_ll), 3)
    d_max_hh = round(_pct(diur_stats["day_max"], pct["hh"] * 100, g_hh), 3)
    d_min_l = round(_pct(diur_stats["day_min"], pct["l"] * 100, g_l), 3)
    d_max_h = round(_pct(diur_stats["day_max"], pct["h"] * 100, g_h), 3)
    d_min_obs = round(float(diur_stats["day_min"].min()), 3) if not diur_stats.empty else g_ll
    d_max_obs = round(float(diur_stats["day_max"].max()), 3) if not diur_stats.empty else g_hh

    warn_low = int(round(pct["l"] * 100))
    warn_high = int(round(pct["h"] * 100))
    alarm_low = int(round(pct["ll"] * 100))
    alarm_high = int(round(pct["hh"] * 100))

    bandas: dict[str, Any] = {
        "method": f"L/H p{warn_low}/p{warn_high} · LL/HH p{alarm_low:02d}/p{alarm_high} por hora y dow",
        "use_dow": cfg.get("ll_hh_use_dow", True),
        "l_pct": warn_low,
        "h_pct": warn_high,
        "ll_pct": alarm_low,
        "hh_pct": alarm_high,
        "dow_groups": ["wd", "sat", "sun"],
    }
    band_cols = ("l", "h", "ll", "hh")
    if all(c in df.columns for c in band_cols):
        from features import dow_key

        work = df.dropna(subset=["l", "h"]).copy()
        if not work.empty:
            work["_dow"] = work["time_utc"].apply(dow_key)
            by_dow = {}
            for key, grp in work.groupby("_dow"):
                by_dow[key] = {
                    "l_mean": round(float(grp["l"].mean()), 3),
                    "h_mean": round(float(grp["h"].mean()), 3),
                    "ll_mean": round(float(grp["ll"].mean()), 3),
                    "hh_mean": round(float(grp["hh"].mean()), 3),
                    "l_min": round(float(grp["l"].min()), 3),
                    "h_max": round(float(grp["h"].max()), 3),
                    "ll_min": round(float(grp["ll"].min()), 3),
                    "hh_max": round(float(grp["hh"].max()), 3),
                    "puntos": int(len(grp)),
                }
            bandas["por_dow"] = by_dow
            last = work.iloc[-1]
            bandas["l_actual"] = round(float(last["l"]), 3)
            bandas["h_actual"] = round(float(last["h"]), 3)
            bandas["ll_actual"] = round(float(last["ll"]), 3)
            bandas["hh_actual"] = round(float(last["hh"]), 3)

    return {
        "method": f"L/H p{warn_low}/p{warn_high} · LL/HH p{alarm_low:02d}/p{alarm_high}",
        "bandas_ll_hh": bandas,
        "global": {"l": g_l, "h": g_h, "ll": g_ll, "hh": g_hh},
        "nocturno": {
            "l": n_min_l,
            "h": n_max_h,
            "ll": n_min_ll,
            "hh": n_max_hh,
            "min_observado": n_min_obs,
            "max_observado": n_max_obs,
            "ventana": f"{cfg.get('night_start_hour', 22)}h–{cfg.get('night_end_hour', 6)}h",
        },
        "diurno": {
            "l": d_min_l,
            "h": d_max_h,
            "ll": d_min_ll,
            "hh": d_max_hh,
            "min_observado": d_min_obs,
            "max_observado": d_max_obs,
            "ventana": f"{cfg.get('night_end_hour', 6)}h–{cfg.get('night_start_hour', 22)}h",
        },
    }


def limits_path_for(point: str, cfg: dict | None = None) -> Path:
    out_dir = BASE_DIR / (cfg or {}).get("model_dir", "output")
    return out_dir / "limits" / f"{point_slug(point)}.json"
