#!/usr/bin/env python3
"""Perfiles por tipo de variable, homólogo y evaluación de riesgo operacional."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from alarm_engine import evaluate_alarm, is_night_now
from nocturnal import night_mask

PROFILES: dict[str, dict[str, Any]] = {
    "caudal_entrada": {
        "label": "Caudal entrada",
        "unit_default": "l/s",
        "riesgos": ["rebalse_estanque", "aporte_bajo"],
        "hh_significa": "Aporte alto — riesgo de llenado excesivo del estanque",
        "ll_significa": "Aporte bajo — estanque puede vaciarse",
        "homolog_alerta": "Cambio vs ayer/semana en el aporte de agua",
        "rotura_relevante": False,
    },
    "caudal_salida": {
        "label": "Caudal salida",
        "unit_default": "l/s",
        "riesgos": ["perdidas", "rotura", "obstruccion"],
        "hh_significa": "Salida alta — posible pérdida o rotura en la red",
        "ll_significa": "Salida baja — posible obstrucción o cierre",
        "homolog_alerta": "Subida nocturna vs ayer suele indicar fuga/rotura",
        "rotura_relevante": True,
    },
    "caudal": {
        "label": "Caudal",
        "unit_default": "l/s",
        "riesgos": ["perdidas", "rotura"],
        "hh_significa": "Caudal alto vs histórico",
        "ll_significa": "Caudal bajo vs histórico",
        "homolog_alerta": "Desvío vs periodo anterior",
        "rotura_relevante": True,
    },
    "volumen": {
        "label": "Volumen estanque",
        "unit_default": "m³",
        "riesgos": ["rebalse", "estanque_vacio"],
        "hh_significa": "Volumen alto — riesgo de rebalse",
        "ll_significa": "Volumen bajo — riesgo de estanque vacío",
        "homolog_alerta": "Desvío vs ayer/semana en el volumen almacenado",
        "rotura_relevante": False,
    },
    "nivel": {
        "label": "Nivel estanque",
        "unit_default": "m",
        "riesgos": ["rebalse", "estanque_vacio"],
        "hh_significa": "Nivel alto — riesgo de rebalse",
        "ll_significa": "Nivel bajo — riesgo de estanque vacío",
        "homolog_alerta": "Nivel fuera de lo esperado vs ayer/semana",
        "rotura_relevante": False,
    },
    "presion": {
        "label": "Presión",
        "unit_default": "bar",
        "riesgos": ["rotura_nocturna", "presion_baja"],
        "hh_significa": "Presión alta — en la noche (bajo consumo) aumenta riesgo de rotura",
        "ll_significa": "Presión mínima operativa",
        "homolog_alerta": "Subida nocturna de presión vs histórico",
        "rotura_relevante": True,
    },
    "otro": {
        "label": "Medición",
        "unit_default": "",
        "riesgos": ["fuera_banda"],
        "hh_significa": "Valor alto vs histórico",
        "ll_significa": "Valor bajo vs histórico",
        "homolog_alerta": "Desvío vs periodo anterior",
        "rotura_relevante": False,
    },
}


def infer_var_type(point: str, unit: str = "") -> str:
    p = point.lower()
    u = (unit or "").lower()
    if "volumen" in p or p.endswith(".vol") or u in ("m3", "m³"):
        return "volumen"
    if "nivel" in p or "level" in p or "altura" in p:
        return "nivel"
    if "presion" in p or "presión" in p or "pressure" in p or u in ("bar", "mbar", "kpa"):
        return "presion"
    if any(x in p for x in ("entrada", "entrad", "inflow", "llenado", "aliment")):
        return "caudal_entrada"
    if any(x in p for x in ("salida", "outflow", "descarga", "peap")):
        return "caudal_salida"
    if "caudal" in p or u in ("l/s", "lps", "m3/h", "m3h"):
        return "caudal_salida"
    return "otro"


def get_profile(point: str, unit: str = "", cfg: dict | None = None) -> dict[str, Any]:
    cfg = cfg or {}
    overrides = (cfg.get("variable_overrides") or {}).get(point, {})
    vtype = overrides.get("type") or infer_var_type(point, unit)
    base = dict(PROFILES.get(vtype, PROFILES["otro"]))
    base["type"] = vtype
    base["point"] = point
    base["unit"] = unit or overrides.get("unit") or base.get("unit_default", "")
    if overrides:
        base.update({k: v for k, v in overrides.items() if k != "type"})
    return base


def attach_homolog(df: pd.DataFrame, ma: int = 15) -> pd.DataFrame:
    """Compara cada punto con el mismo instante hace 1, 7 y 30 días."""
    if df.empty:
        return df.copy()
    out = df.sort_values("time_utc").reset_index(drop=True)
    base = out[["time_utc", "value"]].rename(columns={"value": "_hv"})
    tol = pd.Timedelta(minutes=max(ma * 2, 5))
    for days, col in ((1, "homolog_1d"), (7, "homolog_7d"), (30, "homolog_30d")):
        q = out[["time_utc", "value"]].copy()
        q["_target"] = q["time_utc"] - pd.Timedelta(days=days)
        m = pd.merge_asof(
            q.sort_values("_target"),
            base.sort_values("time_utc"),
            left_on="_target",
            right_on="time_utc",
            direction="nearest",
            tolerance=tol,
        )
        out[col] = m["_hv"].values
        denom = out[col].replace(0, np.nan)
        out[f"dev_{col}_pct"] = ((out["value"] - out[col]) / denom) * 100
    return out


def _pct_change(current: float, reference: float | None) -> float | None:
    if reference is None or pd.isna(reference) or reference == 0:
        return None
    return round(float((current - reference) / reference * 100), 2)


def daily_reference_stats(df: pd.DataFrame, last_n: int = 21) -> list[dict[str, Any]]:
    """
    Estadística diaria con % firmado vs día, semana y mes anterior.
    Útil para nivel de estanque (tendencia de vaciado/llenado).
    """
    if df.empty:
        return []

    work = df.copy()
    work["date_local"] = work["time_local"].dt.date
    daily = (
        work.groupby("date_local", as_index=False)["value"]
        .agg(nivel_medio="mean", nivel_min="min", nivel_max="max", nivel_fin="last")
    )
    daily = daily.sort_values("date_local").reset_index(drop=True)
    rows: list[dict[str, Any]] = []
    for i, row in daily.iterrows():
        ref_d = daily.loc[i - 1, "nivel_medio"] if i >= 1 else None
        ref_w = daily.loc[i - 7, "nivel_medio"] if i >= 7 else None
        ref_m = daily.loc[i - 30, "nivel_medio"] if i >= 30 else None
        rows.append({
            "date_local": str(row["date_local"]),
            "nivel_medio": round(float(row["nivel_medio"]), 3),
            "nivel_min": round(float(row["nivel_min"]), 3),
            "nivel_max": round(float(row["nivel_max"]), 3),
            "nivel_fin": round(float(row["nivel_fin"]), 3),
            "pct_vs_dia": _pct_change(float(row["nivel_medio"]), float(ref_d) if ref_d is not None else None),
            "pct_vs_semana": _pct_change(float(row["nivel_medio"]), float(ref_w) if ref_w is not None else None),
            "pct_vs_mes": _pct_change(float(row["nivel_medio"]), float(ref_m) if ref_m is not None else None),
        })
    return rows[-last_n:]


def evaluate_risk(
    df: pd.DataFrame,
    profile: dict[str, Any],
    cfg: dict,
    *,
    alarm: dict[str, Any] | None = None,
    sixsigma: dict[str, Any] | None = None,
    score: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evalúa riesgo operacional según tipo de variable."""
    if df.empty:
        return {"nivel": 0, "estado": "sin_datos", "reglas": [], "mensaje": "Sin datos"}

    last = df.iloc[-1]
    v = float(last["value"])
    ts = last["time_local"]
    vtype = profile["type"]
    night = is_night_now(ts, cfg.get("night_start_hour", 22), cfg.get("night_end_hour", 6))

    reglas: list[str] = []
    mensajes: list[str] = []
    nivel = 0

    dev1 = float(last["dev_homolog_1d_pct"]) if "dev_homolog_1d_pct" in df.columns and pd.notna(last.get("dev_homolog_1d_pct")) else None
    dev7 = float(last["dev_homolog_7d_pct"]) if "dev_homolog_7d_pct" in df.columns and pd.notna(last.get("dev_homolog_7d_pct")) else None
    dev30 = float(last["dev_homolog_30d_pct"]) if "dev_homolog_30d_pct" in df.columns and pd.notna(last.get("dev_homolog_30d_pct")) else None
    h1 = float(last["homolog_1d"]) if "homolog_1d" in df.columns and pd.notna(last.get("homolog_1d")) else None
    h7 = float(last["homolog_7d"]) if "homolog_7d" in df.columns and pd.notna(last.get("homolog_7d")) else None
    h30 = float(last["homolog_30d"]) if "homolog_30d" in df.columns and pd.notna(last.get("homolog_30d")) else None

    pct_day = float(cfg.get("homolog_pct_day", 20))
    pct_week = float(cfg.get("homolog_pct_week", 25))
    pct_month = float(cfg.get("homolog_pct_month", 25))

    if dev1 is not None and abs(dev1) >= pct_day:
        reglas.append("desvio_homologo_1d")
        mensajes.append(f"Vs ayer: {dev1:+.1f}% (ref {h1})")
    if dev7 is not None and abs(dev7) >= pct_week:
        reglas.append("desvio_homologo_7d")
        mensajes.append(f"Vs semana anterior: {dev7:+.1f}% (ref {h7})")
    if dev30 is not None and abs(dev30) >= pct_month:
        reglas.append("desvio_homologo_30d")
        mensajes.append(f"Vs mes anterior: {dev30:+.1f}% (ref {h30})")

    ll = last.get("ll")
    hh = last.get("hh")
    l_lim = last.get("l")
    h_lim = last.get("h")
    if pd.notna(l_lim) and v < float(l_lim):
        reglas.append("bajo_l")
        mensajes.append(profile.get("ll_significa", f"Valor {v} < L ({l_lim})"))
    if pd.notna(h_lim) and v > float(h_lim):
        reglas.append("sobre_h")
        mensajes.append(profile.get("hh_significa", f"Valor {v} > H ({h_lim})"))
    if pd.notna(ll) and v < float(ll):
        reglas.append("bajo_ll")
        mensajes.append(f"Valor {v} < LL alarma segura ({ll})")
    if pd.notna(hh) and v > float(hh):
        reglas.append("sobre_hh")
        mensajes.append(f"Valor {v} > HH alarma segura ({hh})")

    ss = sixsigma or {}
    ss_sum = ss.get("summary") or {}
    if ss_sum.get("trend_down") and vtype == "nivel":
        reglas.append("tendencia_descendente")
        mensajes.append("Nivel bajando varios días — riesgo de vaciado")
    if ss_sum.get("trend_up") and vtype == "nivel":
        reglas.append("tendencia_ascendente_nivel")
        mensajes.append("Nivel subiendo sostenido — monitorear rebalse")
    if ss_sum.get("trend_up") and vtype in ("caudal_salida", "caudal", "presion"):
        reglas.append("tendencia_ascendente")
        mensajes.append("Tendencia ascendente sostenida (Six Sigma)")
    if ss_sum.get("stuck"):
        reglas.append("puntos_pegados")
        mensajes.append("Puntos pegados — posible sensor congelado")

    if vtype == "presion" and night and pd.notna(h_lim) and v > float(h_lim):
        reglas.append("presion_nocturna_alta")
        mensajes.append("Presión alta en horario nocturno — riesgo de rotura por bajo consumo")
    if vtype in ("nivel", "volumen") and pd.notna(h_lim) and v > float(h_lim):
        reglas.append("riesgo_rebalse")
        mensajes.append("Volumen alto — riesgo de rebalse" if vtype == "volumen" else "Nivel alto — riesgo de rebalse")
    if vtype in ("nivel", "volumen") and pd.notna(l_lim) and v < float(l_lim):
        reglas.append("riesgo_vaciado")
        mensajes.append("Volumen bajo — riesgo de estanque vacío" if vtype == "volumen" else "Nivel bajo — riesgo de estanque vacío")
    if vtype == "caudal_entrada" and pd.notna(h_lim) and v > float(h_lim):
        reglas.append("aporte_excesivo")
        mensajes.append("Entrada alta — monitorear llenado de estanque")
    if vtype == "caudal_salida" and night and pd.notna(h_lim) and v > float(h_lim):
        reglas.append("salida_nocturna_alta")
        mensajes.append("Salida alta de noche — sospecha de pérdidas/rotura")

    prob = (score or {}).get("prob")
    if prob is not None and prob >= 0.5 and profile.get("rotura_relevante"):
        reglas.append("ml_pre_rotura")
        mensajes.append(f"Modelo GPU: prob pre-rotura {prob * 100:.0f}%")

    if alarm and alarm.get("rotura_inmediata"):
        reglas.append("rotura_inmediata")
        mensajes.append(alarm.get("mensaje", "Rotura inmediata detectada"))
    elif alarm and alarm.get("prealarm", {}).get("score", 0) >= 60:
        pa = alarm["prealarm"]
        reglas.append("prealarma_compuesta")
        mensajes.append(f"Score prealarma {pa['score']}/100 ({pa.get('color', '')})")
    elif alarm and (alarm.get("in_alarm") or alarm.get("advertencia")):
        reglas.append("alarma_ll_hh" if alarm.get("in_alarm") else "advertencia_l_h")
        mensajes.append(alarm.get("mensaje", "Alarma L/LL/H/HH"))

    # Nivel 3 — crítico
    crit = {
        "riesgo_rebalse", "riesgo_vaciado", "presion_nocturna_alta",
        "salida_nocturna_alta", "alarma_ll_hh", "rotura_inmediata", "prealarma_compuesta", "sobre_hh", "bajo_ll", "ml_pre_rotura", "tendencia_descendente",
    }
    if any(r in reglas for r in crit) or (reglas.count("tendencia_ascendente") and "sobre_h" in reglas):
        nivel = 3
    elif reglas:
        nivel = 2 if len(reglas) >= 2 or "desvio_homologo_1d" in reglas else 1

    estado = {0: "ok", 1: "vigilancia", 2: "pre_alarma", 3: "alarma"}[nivel]
    return {
        "nivel": nivel,
        "estado": estado,
        "tipo_variable": vtype,
        "tipo_label": profile.get("label"),
        "es_noche": night,
        "reglas": reglas,
        "mensaje": "; ".join(mensajes) if mensajes else "Comportamiento dentro de lo esperado",
        "homolog": {
            "valor_1d": round(h1, 3) if h1 is not None else None,
            "valor_7d": round(h7, 3) if h7 is not None else None,
            "valor_30d": round(h30, 3) if h30 is not None else None,
            "dev_pct_1d": round(dev1, 1) if dev1 is not None else None,
            "dev_pct_7d": round(dev7, 1) if dev7 is not None else None,
            "dev_pct_30d": round(dev30, 1) if dev30 is not None else None,
        },
        "valor": round(v, 3),
        "ts": str(ts),
    }
