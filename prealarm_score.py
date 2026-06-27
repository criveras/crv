#!/usr/bin/env python3
"""Score compuesto de prealarma — percentil, cambio, persistencia, tendencia, correlación."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from anomaly_engine import _median_abs_delta


DEFAULT_WEIGHTS: dict[str, float] = {
    "percentil": 0.30,
    "razon_cambio": 0.25,
    "persistencia": 0.20,
    "tendencia": 0.15,
    "correlacion": 0.10,
}

DEFAULT_THRESHOLDS: dict[str, float] = {
    "amarillo": 60,
    "naranja": 80,
    "rojo": 95,
}


def score_weights(cfg: dict) -> dict[str, float]:
    raw = cfg.get("prealarm_weights") or {}
    w = {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in raw.items()}}
    total = sum(w.values()) or 1.0
    return {k: v / total for k, v in w.items()}


def score_thresholds(cfg: dict) -> dict[str, float]:
    raw = cfg.get("prealarm_thresholds") or {}
    return {**DEFAULT_THRESHOLDS, **{k: float(v) for k, v in raw.items()}}


def _clamp100(x: float) -> float:
    return float(max(0.0, min(100.0, x)))


def _percentil_component(v: float, l: float, h: float, ll: float, hh: float, p50: float) -> float:
    """Desviación respecto a bandas L/H/LL/HH normalizada 0–100."""
    if np.isnan(v):
        return 0.0
    if not np.isnan(hh) and v > hh:
        span = max(abs(hh - (p50 if not np.isnan(p50) else h)), 0.01)
        return _clamp100(80 + (v - hh) / span * 20)
    if not np.isnan(ll) and v < ll:
        span = max(abs((p50 if not np.isnan(p50) else l) - ll), 0.01)
        return _clamp100(80 + (ll - v) / span * 20)
    if not np.isnan(h) and v > h:
        span = max(h - (l if not np.isnan(l) else p50), 0.01)
        return _clamp100((v - h) / span * 70)
    if not np.isnan(l) and v < l:
        span = max((h if not np.isnan(h) else p50) - l, 0.01)
        return _clamp100((l - v) / span * 70)
    return 0.0


def _rate_component(values: np.ndarray, i: int, cfg: dict) -> float:
    if i < 1:
        return 0.0
    factor = float(cfg.get("alarm_rate_factor", 2.5))
    dy = float(values[i] - values[i - 1])
    ref = _median_abs_delta(values[: i + 1])
    if ref <= 0:
        return _clamp100(abs(dy) / max(float(cfg.get("delta_umbral", 3.0)), 0.01) * 50)
    return _clamp100(abs(dy) / (factor * ref) * 55)


def _persist_component(run_warn: int, run_llhh: int, cfg: dict) -> float:
    p_max = int(cfg.get("alarm_persistence_max", 5))
    run = max(run_warn, run_llhh)
    return _clamp100(run / max(p_max, 1) * 100)


def _trend_component(values: np.ndarray, i: int, cfg: dict, above_normal: bool, below_normal: bool) -> float:
    trend_len = int(cfg.get("sixsigma_trend_len", 6))
    if i < 1:
        return 0.0
    up = down = 0
    for j in range(i, max(0, i - trend_len), -1):
        if j < 1:
            break
        if values[j] > values[j - 1]:
            up += 1
            down = 0
        elif values[j] < values[j - 1]:
            down += 1
            up = 0
        else:
            break
    if above_normal and up >= 2:
        return _clamp100(up / trend_len * 100)
    if below_normal and down >= 2:
        return _clamp100(down / trend_len * 100)
    return _clamp100(max(up, down) / trend_len * 40)


def load_related_matrix(df: pd.DataFrame, cfg: dict) -> pd.DataFrame | None:
    """Alinea series relacionadas (config related_points) por tiempo."""
    tags = cfg.get("related_points") or []
    if not tags or df.empty:
        return None
    try:
        from rt3_client import fetch_series
    except ImportError:
        return None

    fini = cfg.get("fini", "*-14d")
    ma = int(cfg.get("ma", 5))
    tz = cfg.get("timezone", "America/Santiago")
    base = df[["time_utc"]].sort_values("time_utc").drop_duplicates()
    tol = pd.Timedelta(minutes=max(ma * 2, 5))
    merged = base.copy()

    for idx, tag in enumerate(tags):
        try:
            rel = fetch_series(tag, fini, "*", ma, tz=tz)
            if rel.empty:
                continue
            rel = rel[["time_utc", "value"]].rename(columns={"value": f"_rel_{idx}"})
            merged = pd.merge_asof(
                merged.sort_values("time_utc"),
                rel.sort_values("time_utc"),
                on="time_utc",
                direction="nearest",
                tolerance=tol,
            )
        except Exception:
            continue

    rel_cols = [c for c in merged.columns if c.startswith("_rel_")]
    if not rel_cols:
        return None
    return merged


def _correlation_component_series(df: pd.DataFrame, i: int, cfg: dict) -> float:
    rel_cols = [c for c in df.columns if c.startswith("_rel_")]
    if not rel_cols or i < 4:
        return 0.0
    window = int(cfg.get("prealarm_corr_window", 12))
    main = df["value"].astype(float).values
    lo = max(1, i - window + 1)
    dy_main = np.diff(main[lo : i + 1])
    if len(dy_main) < 3 or np.std(dy_main) < 1e-9:
        return 0.0

    best = 0.0
    for col in rel_cols:
        rel = df[col].astype(float).values
        dy_rel = np.diff(rel[lo : i + 1])
        n = min(len(dy_main), len(dy_rel))
        if n < 3:
            continue
        a, b = dy_main[-n:], dy_rel[-n:]
        if np.std(a) < 1e-9 or np.std(b) < 1e-9:
            continue
        corr = float(np.corrcoef(a, b)[0, 1])
        if np.isnan(corr):
            continue
        joint = abs(corr) * min(100.0, (np.std(a) + np.std(b)) / (np.std(main[lo:i + 1]) + 1e-9) * 50)
        best = max(best, _clamp100(joint))
    return best


def score_to_status(score: float, thresholds: dict[str, float]) -> tuple[str, int, str]:
    if score >= thresholds["rojo"]:
        return "prealarma_roja", 4, "rojo"
    if score >= thresholds["naranja"]:
        return "prealarma_naranja", 3, "naranja"
    if score >= thresholds["amarillo"]:
        return "prealarma_amarilla", 2, "amarillo"
    return "normal", 0, "normal"


def apply_prealarm_scores(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Calcula score compuesto por punto y actualiza estado de prealarma."""
    if df.empty:
        return df.copy()

    out = df.copy()
    n = len(out)
    weights = score_weights(cfg)
    thresholds = score_thresholds(cfg)
    p_min = int(cfg.get("alarm_persistence_min", 3))

    values = out["value"].astype(float).values
    l_vals = out["l"].values if "l" in out.columns else np.full(n, np.nan)
    h_vals = out["h"].values if "h" in out.columns else np.full(n, np.nan)
    ll_vals = out["ll"].values if "ll" in out.columns else np.full(n, np.nan)
    hh_vals = out["hh"].values if "hh" in out.columns else np.full(n, np.nan)
    p50_vals = out["p50"].values if "p50" in out.columns else np.full(n, np.nan)
    ignored = out["anom_ignored"].values if "anom_ignored" in out.columns else np.zeros(n, dtype=bool)

    rel_df = load_related_matrix(out, cfg)
    if rel_df is not None:
        out = out.merge(rel_df, on="time_utc", how="left")

    scores_pct: list[float] = []
    scores_rate: list[float] = []
    scores_persist: list[float] = []
    scores_trend: list[float] = []
    scores_corr: list[float] = []
    scores_total: list[float] = []
    score_colors: list[str] = []

    run_warn = 0
    run_llhh = 0

    for i in range(n):
        if ignored[i]:
            scores_pct.append(0)
            scores_rate.append(0)
            scores_persist.append(0)
            scores_trend.append(0)
            scores_corr.append(0)
            scores_total.append(0)
            score_colors.append("")
            continue

        v = float(values[i])
        l_v = float(l_vals[i]) if pd.notna(l_vals[i]) else np.nan
        h_v = float(h_vals[i]) if pd.notna(h_vals[i]) else np.nan
        ll_v = float(ll_vals[i]) if pd.notna(ll_vals[i]) else np.nan
        hh_v = float(hh_vals[i]) if pd.notna(hh_vals[i]) else np.nan
        p50 = float(p50_vals[i]) if pd.notna(p50_vals[i]) else np.nan

        below_l = pd.notna(l_v) and v < l_v
        above_h = pd.notna(h_v) and v > h_v
        below_ll = pd.notna(ll_v) and v < ll_v
        above_hh = pd.notna(hh_v) and v > hh_v

        run_warn = run_warn + 1 if (below_l or above_h) else 0
        run_llhh = run_llhh + 1 if (below_ll or above_hh) else 0

        s_pct = _percentil_component(v, l_v, h_v, ll_v, hh_v, p50)
        s_rate = _rate_component(values, i, cfg)
        s_persist = _persist_component(run_warn, run_llhh, cfg)
        s_trend = _trend_component(values, i, cfg, above_h or above_hh, below_l or below_ll)
        s_corr = _correlation_component_series(out, i, cfg)

        total = (
            weights["percentil"] * s_pct
            + weights["razon_cambio"] * s_rate
            + weights["persistencia"] * s_persist
            + weights["tendencia"] * s_trend
            + weights["correlacion"] * s_corr
        )
        total = round(_clamp100(total), 1)

        scores_pct.append(round(s_pct, 1))
        scores_rate.append(round(s_rate, 1))
        scores_persist.append(round(s_persist, 1))
        scores_trend.append(round(s_trend, 1))
        scores_corr.append(round(s_corr, 1))
        scores_total.append(total)
        score_colors.append(score_to_status(total, thresholds)[2])

        cur_status = str(out["anom_status"].iloc[i]) if "anom_status" in out.columns else "normal"
        if cur_status in ("alarma", "rotura_inmediata", "ignorado"):
            continue

        st, lvl, color = score_to_status(total, thresholds)
        if total >= thresholds["amarillo"]:
            limit = "H" if above_h or above_hh else ("L" if below_l or below_ll else "")
            pct_lbl = out["anom_pct"].iloc[i] if "anom_pct" in out.columns else ""
            out.at[out.index[i], "anom_status"] = st
            out.at[out.index[i], "anom_level"] = lvl
            out.at[out.index[i], "anom_limit"] = limit or out.at[out.index[i], "anom_limit"] if "anom_limit" in out.columns else limit
            out.at[out.index[i], "anom_confidence"] = (
                "alta" if total >= thresholds["rojo"] else ("media" if total >= thresholds["naranja"] else "baja")
            )
            color_label = {"amarillo": "Amarillo", "naranja": "Naranjo", "rojo": "Rojo"}.get(color, color)
            out.at[out.index[i], "anom_msg"] = (
                f"PREALARMA {color_label} — Score {total:.0f}/100 "
                f"(pct {s_pct:.0f}%, Δ {s_rate:.0f}%, pers {s_persist:.0f}%, "
                f"tend {s_trend:.0f}%, corr {s_corr:.0f}%)."
            )
        elif cur_status in ("advertencia", "pre_alarma") and total < thresholds["amarillo"]:
            out.at[out.index[i], "anom_status"] = "normal"
            out.at[out.index[i], "anom_level"] = 0
            out.at[out.index[i], "anom_msg"] = f"Dentro de rango normal (score {total:.0f})."

    out["prealarm_score"] = scores_total
    out["prealarm_pct"] = scores_pct
    out["prealarm_rate"] = scores_rate
    out["prealarm_persist"] = scores_persist
    out["prealarm_trend"] = scores_trend
    out["prealarm_corr"] = scores_corr
    out["prealarm_color"] = score_colors
    return out


def current_prealarm_summary(row: pd.Series, cfg: dict) -> dict[str, Any]:
    """Resumen del score compuesto para el punto actual."""
    thresholds = score_thresholds(cfg)
    score = float(row.get("prealarm_score") or 0)
    _, lvl, color = score_to_status(score, thresholds)
    return {
        "score": round(score, 1),
        "color": color,
        "nivel": lvl,
        "componentes": {
            "percentil": round(float(row.get("prealarm_pct") or 0), 1),
            "razon_cambio": round(float(row.get("prealarm_rate") or 0), 1),
            "persistencia": round(float(row.get("prealarm_persist") or 0), 1),
            "tendencia": round(float(row.get("prealarm_trend") or 0), 1),
            "correlacion": round(float(row.get("prealarm_corr") or 0), 1),
        },
        "pesos": score_weights(cfg),
        "umbrales": thresholds,
    }
