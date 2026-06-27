#!/usr/bin/env python3
"""Análisis de anomalías L/LL/H/HH con persistencia, pendiente y calidad de dato."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _cfg_pct(cfg: dict, key: str, default: float) -> float:
    return float(cfg.get(key, default)) / 100.0


def anomaly_percentiles(cfg: dict) -> dict[str, float]:
    """Percentiles fraccionarios para L/H (advertencia) y LL/HH (alarma segura)."""
    return {
        "l": _cfg_pct(cfg, "warn_low_pct", 10),
        "h": _cfg_pct(cfg, "warn_high_pct", 90),
        "ll": _cfg_pct(cfg, "alarm_low_pct", 2),
        "hh": _cfg_pct(cfg, "alarm_high_pct", 98),
    }


def _stale_mask(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Marca puntos sin actualización reciente (gap anormal entre muestras)."""
    if df.empty or "time_utc" not in df.columns:
        return pd.Series(dtype=bool)
    ma = int(cfg.get("ma", 15))
    max_gap = pd.Timedelta(minutes=max(ma * 3, 45))
    gaps = df["time_utc"].diff()
    stale = gaps > max_gap
    stale.iloc[0] = False
    return stale.fillna(False)


def _stuck_mask(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Marca puntos pegados (mismo valor repetido)."""
    eps = float(cfg.get("sixsigma_stuck_eps", 0.02))
    min_stuck = int(cfg.get("sixsigma_stuck_min", 5))
    y = df["value"].astype(float).values
    n = len(y)
    stuck = np.zeros(n, dtype=bool)
    run = 1
    for i in range(1, n):
        if abs(y[i] - y[i - 1]) <= eps:
            run += 1
            if run >= min_stuck:
                stuck[i - run + 1 : i + 1] = True
        else:
            run = 1
    return pd.Series(stuck, index=df.index)


def _median_abs_delta(values: np.ndarray, window: int = 24) -> float:
    if len(values) < 2:
        return 0.0
    deltas = np.abs(np.diff(values[-window:]))
    if len(deltas) == 0:
        return 0.0
    med = float(np.median(deltas))
    return med if med > 0 else float(np.mean(deltas))


def _steep_change(values: np.ndarray, i: int, cfg: dict) -> tuple[bool, float | None]:
    """Detecta cambio brusco de pendiente o razón de cambio anormal."""
    if i < 1:
        return False, None
    factor = float(cfg.get("alarm_rate_factor", 2.5))
    dy = float(values[i] - values[i - 1])
    ref = _median_abs_delta(values[: i + 1])
    if ref <= 0:
        return abs(dy) > float(cfg.get("delta_umbral", 3.0)), round(dy, 3)
    steep = abs(dy) > factor * ref
    return steep, round(dy, 3)


def _confidence(persist: int, steep: bool, cfg: dict) -> str:
    p_min = int(cfg.get("alarm_persistence_min", 3))
    p_max = int(cfg.get("alarm_persistence_max", 5))
    if persist >= p_max or (persist >= p_min and steep):
        return "alta"
    if persist >= p_min:
        return "media"
    return "baja"


def _limit_label(side: str, pct: float) -> str:
    pct_int = int(round(pct * 100))
    return f"{side} p{pct_int:02d}" if pct_int < 10 else f"{side} p{pct_int}"


def _rupture_relevant(profile: dict | None) -> bool:
    if not profile:
        return True
    return profile.get("type") in ("presion", "caudal_salida", "caudal") and profile.get("rotura_relevante", True)


def _mark_immediate_ruptures(
    out: pd.DataFrame,
    cfg: dict,
    profile: dict | None = None,
) -> pd.DataFrame:
    """
    Rotura inmediata: presión sobre HH seguida de caída brusca bajo HH.

    Patrón típico de rotura de tubería — pico de presión y luego colapso al
    escaparse el agua.
    """
    if out.empty or not _rupture_relevant(profile):
        out["rotura_inmediata"] = False
        return out

    lookback = int(cfg.get("rupture_lookback_steps", 12))
    min_spike = int(cfg.get("rupture_spike_min", 1))
    min_drop = float(cfg.get("rupture_min_drop", cfg.get("delta_umbral", 3.0)))
    drop_factor = float(cfg.get("rupture_drop_factor", cfg.get("alarm_rate_factor", 2.5)))

    n = len(out)
    values = out["value"].astype(float).values
    hh_vals = out["hh"].values if "hh" in out.columns else np.full(n, np.nan)
    h_vals = out["h"].values if "h" in out.columns else np.full(n, np.nan)
    ignored = out["anom_ignored"].values if "anom_ignored" in out.columns else np.zeros(n, dtype=bool)

    flags = np.zeros(n, dtype=bool)
    statuses = out["anom_status"].tolist() if "anom_status" in out.columns else ["normal"] * n
    levels = out["anom_level"].tolist() if "anom_level" in out.columns else [0] * n
    limits = out["anom_limit"].tolist() if "anom_limit" in out.columns else [""] * n
    pcts = out["anom_pct"].tolist() if "anom_pct" in out.columns else [""] * n
    durations = out["anom_duration"].tolist() if "anom_duration" in out.columns else [0] * n
    rates = out["anom_rate"].tolist() if "anom_rate" in out.columns else [None] * n
    confidences = out["anom_confidence"].tolist() if "anom_confidence" in out.columns else [""] * n
    messages = out["anom_msg"].tolist() if "anom_msg" in out.columns else [""] * n
    pct = anomaly_percentiles(cfg)
    hh_label = _limit_label("HH", pct["hh"])

    for i in range(1, n):
        if ignored[i]:
            continue
        lo = max(0, i - lookback)
        ref_hh = float(hh_vals[lo]) if pd.notna(hh_vals[lo]) else np.nan
        ref_h = float(h_vals[lo]) if "h" in out.columns and pd.notna(h_vals[lo]) else np.nan
        if np.isnan(ref_hh) and i > lo:
            ref_hh = float(np.nanpercentile(hh_vals[lo:i], 98))
        if np.isnan(ref_hh) and not np.isnan(ref_h):
            ref_hh = ref_h

        spike_idxs = [
            j for j in range(lo, i)
            if not ignored[j]
            and (
                (pd.notna(hh_vals[j]) and values[j] > float(max(hh_vals[j], ref_hh)))
                or (not np.isnan(ref_h) and values[j] > ref_h + min_drop * 0.5)
            )
        ]
        if len(spike_idxs) < min_spike:
            continue

        peak_j = max(spike_idxs, key=lambda j: values[j])
        peak_val = float(values[peak_j])
        v = float(values[i])

        dy = v - float(values[i - 1])
        ref = _median_abs_delta(values[: i + 1])
        steep_drop = dy < 0 and (ref <= 0 or abs(dy) > drop_factor * ref)
        below_ref = (not np.isnan(ref_hh) and v < ref_hh) or (not np.isnan(ref_h) and v < ref_h)
        drop_from_peak = peak_val - v

        if steep_drop and below_ref and drop_from_peak >= min_drop:
            flags[i] = True
            spike_steps = len(spike_idxs)
            statuses[i] = "rotura_inmediata"
            levels[i] = 5
            limits[i] = "ROTURA"
            pcts[i] = hh_label
            durations[i] = spike_steps
            rates[i] = round(dy, 3)
            confidences[i] = "alta"
            messages[i] = (
                f"ROTURA INMEDIATA: presión sobre {hh_label} ({spike_steps} muestras) "
                f"seguida de caída brusca bajo HH (Δ={dy:.2f}). Confianza alta."
            )

    out["rotura_inmediata"] = flags
    out["anom_status"] = statuses
    out["anom_level"] = levels
    out["anom_limit"] = limits
    out["anom_pct"] = pcts
    out["anom_duration"] = durations
    out["anom_rate"] = rates
    out["anom_confidence"] = confidences
    out["anom_msg"] = messages
    return out


def analyze_series(df: pd.DataFrame, cfg: dict, profile: dict | None = None) -> pd.DataFrame:
    """
    Evalúa cada punto con reglas L/LL/H/HH.

    - L/H (p10/p90): advertencia temprana
    - LL/HH (p02/p98): alarma segura con persistencia
    - Ignora puntos nulos, pegados o sin actualización
    """
    if df.empty:
        return df.copy()

    pct = anomaly_percentiles(cfg)
    out = df.copy()
    n = len(out)
    stale = _stale_mask(out, cfg)
    stuck = _stuck_mask(out, cfg)

    statuses: list[str] = []
    levels: list[int] = []
    limits: list[str] = []
    pcts: list[str] = []
    durations: list[int] = []
    rates: list[float | None] = []
    confidences: list[str] = []
    messages: list[str] = []
    ignored: list[bool] = []
    steep_flags: list[bool] = []

    values = out["value"].astype(float).values
    ll_vals = out["ll"].values if "ll" in out.columns else np.full(n, np.nan)
    hh_vals = out["hh"].values if "hh" in out.columns else np.full(n, np.nan)
    l_vals = out["l"].values if "l" in out.columns else np.full(n, np.nan)
    h_vals = out["h"].values if "h" in out.columns else np.full(n, np.nan)

    p_min = int(cfg.get("alarm_persistence_min", 3))
    run_low = 0
    run_high = 0

    for i in range(n):
        v = values[i]
        bad = (
            pd.isna(v)
            or bool(stuck.iloc[i])
            or bool(stale.iloc[i])
        )
        if bad:
            statuses.append("ignorado")
            levels.append(0)
            limits.append("")
            pcts.append("")
            durations.append(0)
            rates.append(None)
            confidences.append("")
            messages.append("Dato ignorado (nulo, pegado o sin actualización)")
            ignored.append(True)
            steep_flags.append(False)
            run_low = 0
            run_high = 0
            continue

        l_v = l_vals[i]
        h_v = h_vals[i]
        ll_v = ll_vals[i]
        hh_v = hh_vals[i]
        steep, rate = _steep_change(values, i, cfg)
        steep_flags.append(steep)

        below_ll = pd.notna(ll_v) and v < float(ll_v)
        above_hh = pd.notna(hh_v) and v > float(hh_v)
        below_l = pd.notna(l_v) and v < float(l_v)
        above_h = pd.notna(h_v) and v > float(h_v)

        run_low = run_low + 1 if below_ll else 0
        run_high = run_high + 1 if above_hh else 0
        persist = run_low if below_ll else (run_high if above_hh else 0)

        status = "normal"
        level = 0
        limit = ""
        pct_label = ""
        dur = 0
        conf = ""
        msg = "Dentro de rango normal"

        if below_ll and persist >= p_min:
            status = "alarma"
            level = 3
            limit = "LL"
            pct_label = _limit_label("LL", pct["ll"])
            dur = run_low
            conf = _confidence(run_low, steep, cfg)
            if steep:
                level = 4
            msg = (
                f"ALARMA SEGURA LL: valor bajo {pct_label} durante {run_low} muestras consecutivas."
            )
            if steep:
                msg += " Cambio brusco detectado."
            msg += f" Confianza {conf}."
        elif above_hh and persist >= p_min:
            status = "alarma"
            level = 3
            limit = "HH"
            pct_label = _limit_label("HH", pct["hh"])
            dur = run_high
            conf = _confidence(run_high, steep, cfg)
            if steep:
                level = 4
            msg = (
                f"ALARMA SEGURA HH: valor sobre {pct_label} durante {run_high} muestras consecutivas."
            )
            if steep:
                msg += " Cambio brusco detectado."
            msg += f" Confianza {conf}."
        elif below_ll or above_hh:
            status = "pre_alarma"
            level = 2
            limit = "LL" if below_ll else "HH"
            pct_label = _limit_label(limit, pct["ll" if below_ll else "hh"])
            dur = run_low if below_ll else run_high
            conf = "baja"
            msg = (
                f"Fuera de {limit} ({pct_label}) — {dur}/{p_min} muestras para alarma segura."
            )
            if steep:
                level = 3
                msg += " Cambio brusco detectado."
        elif below_l or above_h:
            status = "advertencia"
            level = 1
            limit = "L" if below_l else "H"
            pct_label = _limit_label(limit, pct["l" if below_l else "h"])
            msg = f"ADVERTENCIA {limit}: valor fuera de {pct_label}."
            if steep:
                level = 2
                msg += " Cambio brusco detectado."
            if below_l and pd.notna(ll_v):
                dur = 0
            if above_h and pd.notna(hh_v):
                dur = 0

        statuses.append(status)
        levels.append(level)
        limits.append(limit)
        pcts.append(pct_label)
        durations.append(dur)
        rates.append(rate)
        confidences.append(conf)
        messages.append(msg)
        ignored.append(False)

    out["anom_status"] = statuses
    out["anom_level"] = levels
    out["anom_limit"] = limits
    out["anom_pct"] = pcts
    out["anom_duration"] = durations
    out["anom_rate"] = rates
    out["anom_confidence"] = confidences
    out["anom_msg"] = messages
    out["anom_ignored"] = ignored
    out["anom_steep"] = steep_flags
    from prealarm_score import apply_prealarm_scores

    out = apply_prealarm_scores(out, cfg)
    return _mark_immediate_ruptures(out, cfg, profile)


def evaluate_current(df: pd.DataFrame, cfg: dict, profile: dict | None = None) -> dict[str, Any]:
    """Evalúa el último punto válido de la serie analizada."""
    if df.empty:
        return {
            "in_alarm": False,
            "blink": False,
            "tipo": "",
            "nivel": 0,
            "franja": "bandas",
            "l_activo": None,
            "h_activo": None,
            "ll_activo": None,
            "hh_activo": None,
            "valor": None,
            "ts": "",
            "mensaje": "Sin datos",
            "advertencia": False,
            "confianza": "",
            "duracion": 0,
            "razon_cambio": None,
            "percentil": "",
        }

    work = analyze_series(df, cfg, profile)
    last = work.iloc[-1]
    v = float(last["value"])
    ts = last["time_local"]
    level = int(last["anom_level"])
    status = str(last["anom_status"])

    rotura = status == "rotura_inmediata"
    in_alarm = rotura or status == "alarma" or status == "prealarma_roja"
    advertencia = status in ("advertencia", "pre_alarma", "prealarma_amarilla", "prealarma_naranja")

    from prealarm_score import current_prealarm_summary

    prealarm = current_prealarm_summary(last, cfg)

    return {
        "in_alarm": in_alarm,
        "blink": in_alarm and level >= 3,
        "rotura_inmediata": rotura,
        "tipo": "ROTURA" if rotura else (str(last["anom_limit"]) if in_alarm else ("ADV_" + str(last["anom_limit"]) if advertencia else "")),
        "nivel": level,
        "franja": "bandas",
        "l_activo": round(float(last["l"]), 3) if pd.notna(last.get("l")) else None,
        "h_activo": round(float(last["h"]), 3) if pd.notna(last.get("h")) else None,
        "ll_activo": round(float(last["ll"]), 3) if pd.notna(last.get("ll")) else None,
        "hh_activo": round(float(last["hh"]), 3) if pd.notna(last.get("hh")) else None,
        "valor": round(v, 3),
        "ts": str(ts),
        "mensaje": str(last["anom_msg"]),
        "advertencia": advertencia,
        "confianza": str(last["anom_confidence"]),
        "duracion": int(last["anom_duration"]),
        "razon_cambio": last["anom_rate"] if pd.notna(last.get("anom_rate")) else None,
        "percentil": str(last["anom_pct"]),
        "cambio_brusco": bool(last.get("anom_steep")),
        "ignorado": bool(last.get("anom_ignored")),
        "prealarm": prealarm,
    }


def series_point_payload(row: pd.Series) -> dict[str, Any]:
    """Metadatos de anomalía para un punto del gráfico."""
    return {
        "anom_status": row.get("anom_status"),
        "anom_level": int(row["anom_level"]) if pd.notna(row.get("anom_level")) else 0,
        "anom_limit": row.get("anom_limit") or "",
        "anom_pct": row.get("anom_pct") or "",
        "anom_duration": int(row["anom_duration"]) if pd.notna(row.get("anom_duration")) else 0,
        "anom_rate": round(float(row["anom_rate"]), 3) if pd.notna(row.get("anom_rate")) else None,
        "anom_confidence": row.get("anom_confidence") or "",
        "anom_msg": row.get("anom_msg") or "",
        "rotura_inmediata": bool(row.get("rotura_inmediata")),
        "prealarm_score": round(float(row["prealarm_score"]), 1) if pd.notna(row.get("prealarm_score")) else None,
        "prealarm_color": row.get("prealarm_color") or "",
        "prealarm_pct": round(float(row["prealarm_pct"]), 1) if pd.notna(row.get("prealarm_pct")) else None,
        "prealarm_rate": round(float(row["prealarm_rate"]), 1) if pd.notna(row.get("prealarm_rate")) else None,
        "prealarm_persist": round(float(row["prealarm_persist"]), 1) if pd.notna(row.get("prealarm_persist")) else None,
        "prealarm_trend": round(float(row["prealarm_trend"]), 1) if pd.notna(row.get("prealarm_trend")) else None,
        "prealarm_corr": round(float(row["prealarm_corr"]), 1) if pd.notna(row.get("prealarm_corr")) else None,
    }
