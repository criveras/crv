#!/usr/bin/env python3
"""Evaluación de alarma por bandas L/LL/H/HH (hora + día semana) y límites de respaldo."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from anomaly_engine import evaluate_current
from nocturnal import night_mask


def _hour_local(ts: pd.Timestamp | datetime | str) -> int:
    if isinstance(ts, str):
        ts = pd.Timestamp(ts)
    elif isinstance(ts, datetime):
        ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.hour)


def is_night_now(
    ts: pd.Timestamp | datetime | str,
    start_hour: int = 22,
    end_hour: int = 6,
) -> bool:
    h = _hour_local(ts)
    if start_hour > end_hour:
        return h >= start_hour or h < end_hour
    return start_hour <= h < end_hour


def active_band(limits: dict[str, Any], ts: pd.Timestamp | datetime | str, cfg: dict) -> tuple[str, dict[str, float]]:
    """Devuelve ('nocturno'|'diurno', {l, h, ll, hh})."""
    if is_night_now(ts, cfg.get("night_start_hour", 22), cfg.get("night_end_hour", 6)):
        band = limits.get("nocturno", {})
        return "nocturno", {
            "l": band.get("l"),
            "h": band.get("h"),
            "ll": band.get("ll"),
            "hh": band.get("hh"),
        }
    band = limits.get("diurno", {})
    return "diurno", {
        "l": band.get("l"),
        "h": band.get("h"),
        "ll": band.get("ll"),
        "hh": band.get("hh"),
    }


def evaluate_alarm(
    value: float,
    ts: pd.Timestamp | datetime | str,
    limits: dict[str, Any],
    cfg: dict,
    band_ll: float | None = None,
    band_hh: float | None = None,
    band_l: float | None = None,
    band_h: float | None = None,
    df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """
    Compara valor actual con L/LL/H/HH de la banda horaria (incl. sáb/dom).
    Si se provee df, usa persistencia y detección de cambio brusco.
    """
    if df is not None and not df.empty:
        return evaluate_current(df, cfg)

    franja, band = active_band(limits, ts, cfg)
    l = band_l if band_l is not None else band.get("l")
    h = band_h if band_h is not None else band.get("h")
    ll = band_ll if band_ll is not None else band.get("ll")
    hh = band_hh if band_hh is not None else band.get("hh")

    in_alarm = False
    advertencia = False
    tipo = ""
    nivel = 0
    msg = "Dentro de límites"

    if ll is not None and value < ll:
        in_alarm = True
        tipo = "LL"
        nivel = 3
        msg = f"Valor {value} < LL banda ({ll})"
    elif hh is not None and value > hh:
        in_alarm = True
        tipo = "HH"
        nivel = 3
        msg = f"Valor {value} > HH banda ({hh})"
    elif l is not None and value < l:
        advertencia = True
        tipo = "L"
        nivel = 1
        msg = f"Advertencia: valor {value} < L banda ({l})"
    elif h is not None and value > h:
        advertencia = True
        tipo = "H"
        nivel = 1
        msg = f"Advertencia: valor {value} > H banda ({h})"

    return {
        "in_alarm": in_alarm,
        "blink": in_alarm and nivel >= 3,
        "advertencia": advertencia,
        "tipo": tipo,
        "nivel": nivel,
        "franja": franja,
        "l_activo": l,
        "h_activo": h,
        "ll_activo": ll,
        "hh_activo": hh,
        "bandas_metodo": limits.get("bandas_ll_hh", {}).get("method"),
        "valor": round(float(value), 3),
        "ts": str(ts),
        "mensaje": msg,
        "confianza": "media" if in_alarm else ("baja" if advertencia else ""),
        "duracion": 0,
        "razon_cambio": None,
        "percentil": "",
        "cambio_brusco": False,
        "ignorado": False,
    }
