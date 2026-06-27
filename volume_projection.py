#!/usr/bin/env python3
"""Proyección de volumen en estanques — curva ideal y simulación por caudal (demo_ia)."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DEMO_IA_EXPORT_DIR = BASE_DIR / "demo_ia" / "exports"
CHILE_TZ = ZoneInfo("America/Santiago")
SLOT_SECONDS = 900.0
LS_TO_M3 = SLOT_SECONDS / 1000.0

RECINTO_ALIASES: dict[str, str] = {
    "capi": "tk_capi",
    "plg": "tk_plg",
    "paipote": "tk_paipote",
    "mr": "tk_mr",
    "rosario": "tk_rosario",
    "tamarilla": "tk_tamarilla",
    "escorial": "tk_escorial",
    "crayada": "tk_crayada",
    "cray": "tk_crayada",
    "copa": "tk_copa",
}

QOUT_KEY = "qout_ia"
QOUT_PROFILE_LABEL = "perfil_consumo_15min.qout_ia"


def is_volume_point(point: str) -> bool:
    p = (point or "").lower()
    return "volumen" in p or p.endswith(".vol")


def resolve_recinto(point: str, cfg: dict | None = None) -> str | None:
    cfg = cfg or {}
    explicit = (cfg.get("volume_recintos") or {}).get(point)
    if explicit:
        return str(explicit)
    p = point.lower()
    for key, recinto in RECINTO_ALIASES.items():
        if key in p:
            return recinto
    return None


def _flow_tags_from_sqlite(sqlite_path: str | Path) -> dict[str, str | None]:
    path = Path(sqlite_path)
    if not path.is_file():
        return {}
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='recintos'")
        if not cur.fetchone():
            return {}
        cur.execute("SELECT point_qin1, point_qin2, point_vol, point_qout FROM recintos LIMIT 1")
        row = cur.fetchone()
        if not row:
            return {}
        return {
            "qin": row[0] if row[0] else None,
            "qin2": row[1] if row[1] else None,
            "vol": row[2] if row[2] else None,
            "qout": row[3] if row[3] else None,
        }
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _infer_qin_point(recinto: str) -> str | None:
    if not recinto.startswith("tk_"):
        return None
    alias = recinto[3:]
    if alias in ("tamarilla", "escorial", "crayada", "copa"):
        return None
    if alias == "rosario":
        return "cp.rosario.nuevo.entrada_caudal"
    return f"cp.{alias}.caudal_entrada"


def resolve_flow_tags(point: str, recinto: str, snapshot: dict[str, Any], cfg: dict | None = None) -> dict[str, str | None]:
    cfg = cfg or {}
    flow_cfg = cfg.get("volume_flow_tags") or {}
    tags: dict[str, str | None] = {}
    for key in ("qin", "qin2", "vol", "qout"):
        val = (flow_cfg.get(point) or {}).get(key) if isinstance(flow_cfg.get(point), dict) else None
        if not val and isinstance(flow_cfg.get(recinto), dict):
            val = flow_cfg[recinto].get(key)
        if val:
            tags[key] = str(val)

    meta = snapshot.get("meta") or {}
    sqlite_path = meta.get("sqlite_path")
    if sqlite_path:
        for key, val in _flow_tags_from_sqlite(sqlite_path).items():
            if val and not tags.get(key):
                tags[key] = val

    if not tags.get("qin"):
        inferred = _infer_qin_point(recinto)
        if inferred:
            tags["qin"] = inferred
    if not tags.get("vol") and is_volume_point(point):
        tags["vol"] = point
    return tags


def _qout_label(flow_tags: dict[str, str | None], snapshot: dict[str, Any]) -> str:
    qout_point = flow_tags.get("qout")
    if qout_point:
        return str(qout_point)
    meta = snapshot.get("meta") or {}
    model_kind = meta.get("model_kind") or "IA"
    return f"{QOUT_PROFILE_LABEL} (modelo {model_kind})"


def _fetch_live_qin(qin_point: str | None, cfg: dict | None) -> tuple[float | None, str | None]:
    if not qin_point:
        return None, None
    try:
        from rt3_client import fetch_series

        cfg = cfg or {}
        tz = cfg.get("timezone", "America/Santiago")
        ma = int(cfg.get("ma", 15))
        df = fetch_series(qin_point, "*-6h", "*", ma, tz=tz)
        if df.empty:
            return None, None
        last = df.iloc[-1]
        ts = last["time_local"]
        fecha = ts.isoformat(timespec="minutes") if hasattr(ts, "isoformat") else str(ts)
        return float(last["value"]), fecha
    except Exception:
        return None, None


def _load_export(recinto: str) -> dict[str, Any] | None:
    path = DEMO_IA_EXPORT_DIR / f"{recinto}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("snapshot") or data


def slot_index_from_datetime(dt: datetime) -> int:
    return dt.hour * 4 + (dt.minute // 15)


def profile_group_for_day(dt: datetime) -> str:
    wd = dt.weekday()
    if wd >= 5:
        return "weekend"
    return "weekday"


def _perfil_from_sqlite(sqlite_path: str | Path, profile_type: str = "weekday") -> list[dict[str, Any]]:
    path = Path(sqlite_path)
    if not path.is_file():
        return []
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(perfil_consumo_15min)")
        cols = [row[1] for row in cur.fetchall()]
        if not cols:
            return []
        has_profile = "profile_type" in cols
        if has_profile:
            cur.execute(
                """
                SELECT slot_index, hora_texto, qout_promedio, qout_ia
                FROM perfil_consumo_15min
                WHERE profile_type = ?
                ORDER BY slot_index
                """,
                (profile_type,),
            )
        else:
            cur.execute(
                """
                SELECT slot_index, hora_texto, qout_promedio, qout_ia
                FROM perfil_consumo_15min
                ORDER BY slot_index
                """
            )
        rows: list[dict[str, Any]] = []
        for slot_index, hora_texto, qout_promedio, qout_ia in cur.fetchall():
            rows.append(
                {
                    "slot_index": int(slot_index),
                    "hora_texto": str(hora_texto),
                    "qout_promedio": float(qout_promedio) if qout_promedio is not None else None,
                    "qout_ia": float(qout_ia) if qout_ia is not None else None,
                }
            )
        return rows
    finally:
        conn.close()


def _perfil_from_export(snapshot: dict[str, Any], profile_type: str = "weekday") -> list[dict[str, Any]]:
    charts = snapshot.get("charts") or {}
    perfil_block = charts.get("perfil_consumo") or {}
    qout_vals = perfil_block.get("ia_profile_selected") or perfil_block.get("qout_ia") or []
    labels = perfil_block.get("labels") or []
    if not qout_vals:
        return []
    rows: list[dict[str, Any]] = []
    for idx, qout in enumerate(qout_vals):
        if qout is None:
            continue
        rows.append(
            {
                "slot_index": idx % 96,
                "hora_texto": labels[idx] if idx < len(labels) else f"{idx // 4:02d}:{(idx % 4) * 15:02d}",
                "qout_promedio": float(qout),
                "qout_ia": float(qout),
            }
        )
    return rows


def load_consumption_profile(recinto: str, profile_type: str = "weekday") -> list[dict[str, Any]]:
    snapshot = _load_export(recinto)
    if not snapshot:
        return []
    meta = snapshot.get("meta") or {}
    sqlite_path = meta.get("sqlite_path")
    if sqlite_path:
        rows = _perfil_from_sqlite(sqlite_path, profile_type)
        if rows:
            return rows
        if profile_type == "weekend":
            sat = _perfil_from_sqlite(sqlite_path, "saturday")
            sun = _perfil_from_sqlite(sqlite_path, "sunday")
            if sat or sun:
                return _merge_weekend_profiles(sat, sun)
    return _perfil_from_export(snapshot, profile_type)


def _merge_weekend_profiles(saturday: list[dict], sunday: list[dict]) -> list[dict]:
    by_slot: dict[int, list[dict]] = {}
    for rows in (saturday, sunday):
        for row in rows:
            by_slot.setdefault(int(row["slot_index"]), []).append(row)
    out: list[dict] = []
    for slot in sorted(by_slot):
        slot_rows = by_slot[slot]
        qout_ia = [float(r["qout_ia"]) for r in slot_rows if r.get("qout_ia") is not None]
        qout_avg = [float(r["qout_promedio"]) for r in slot_rows if r.get("qout_promedio") is not None]
        out.append(
            {
                "slot_index": slot,
                "hora_texto": str(slot_rows[0].get("hora_texto", "")),
                "qout_promedio": (sum(qout_avg) / len(qout_avg)) if qout_avg else None,
                "qout_ia": (sum(qout_ia) / len(qout_ia)) if qout_ia else None,
            }
        )
    return out


def build_sinusoidal_ideal_volume_series(
    perfil: list[dict[str, Any]],
    qout_key: str,
    vol_min: float | None,
    vol_max: float | None,
) -> list[float | None]:
    if not perfil or vol_min is None or vol_max is None:
        return [None for _ in perfil]

    slot_samples: dict[int, list[float]] = {}
    fallback: list[tuple[int, float]] = []
    for idx, row in enumerate(perfil):
        value = row.get(qout_key)
        if value is None:
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        slot_raw = row.get("slot_index")
        try:
            slot = int(slot_raw) % 96
            slot_samples.setdefault(slot, []).append(v)
        except (TypeError, ValueError):
            fallback.append((idx % 96, v))

    qout_by_slot: dict[int, float] = {}
    if slot_samples:
        for slot, values in slot_samples.items():
            if values:
                qout_by_slot[slot] = sum(values) / len(values)
    elif fallback:
        for slot, v in fallback:
            qout_by_slot[slot] = v

    if not qout_by_slot:
        return [None for _ in perfil]

    min_idx, min_q = min(qout_by_slot.items(), key=lambda item: item[1])
    max_idx, max_q = max(qout_by_slot.items(), key=lambda item: item[1])
    if max_q <= min_q:
        midpoint = (float(vol_min) + float(vol_max)) / 2.0
        return [midpoint for _ in perfil]

    center = (float(vol_min) + float(vol_max)) / 2.0
    amplitude = (float(vol_max) - float(vol_min)) / 2.0
    omega = (2.0 * math.pi) / 96.0
    span = (max_idx - min_idx) % 96
    phase = (min_idx + (span / 2.0)) % 96

    out: list[float | None] = []
    for idx, row in enumerate(perfil):
        slot_raw = row.get("slot_index")
        try:
            daily_idx = int(slot_raw) % 96
        except (TypeError, ValueError):
            daily_idx = idx % 96
        value = center + amplitude * math.cos(omega * (daily_idx - phase))
        out.append(max(float(vol_min), min(float(vol_max), value)))
    return out


def simulate_volume_projection(
    initial_volume: float | None,
    qin_fixed: float,
    projection_rows: list[dict[str, Any]],
    qout_key: str = "qout_ia",
) -> list[dict[str, Any]]:
    if initial_volume is None:
        return []
    vol = max(0.0, float(initial_volume))
    out: list[dict[str, Any]] = []
    for row in projection_rows:
        qout = row.get(qout_key)
        qout_val = float(qout) if qout is not None else 0.0
        vol = max(0.0, vol + ((float(qin_fixed) - qout_val) * LS_TO_M3))
        out.append({"timestamp": row["timestamp"], "volumen": vol, "qout": qout})
    return out


def compute_required_fixed_qin(
    current_volume: float | None,
    target_volume: float | None,
    projection_rows: list[dict[str, Any]],
    qout_key: str = "qout_ia",
) -> float | None:
    if current_volume is None or target_volume is None or not projection_rows:
        return None
    steps = len(projection_rows)
    total_qout_m3 = 0.0
    for row in projection_rows:
        qout = row.get(qout_key)
        total_qout_m3 += (float(qout) if qout is not None else 0.0) * LS_TO_M3
    required_total_in_m3 = (float(target_volume) - float(current_volume)) + total_qout_m3
    return max(0.0, required_total_in_m3 / (steps * LS_TO_M3))


def compute_constrained_qin_ideal(
    initial_volume: float | None,
    target_upper: float | None,
    projection_rows: list[dict[str, Any]],
    qout_key: str = "qout_ia",
    qin_cap: float | None = None,
) -> float | None:
    if initial_volume is None or target_upper is None or not projection_rows:
        return None

    cap = float(qin_cap) if qin_cap is not None and qin_cap > 0 else 200.0
    target_upper_f = float(target_upper)
    peak_tol = 1e-6
    at_current_peak = float(initial_volume) >= (target_upper_f - peak_tol)

    def projected_peak_metric(qin_value: float) -> float:
        vol = float(initial_volume)
        max_vol = vol
        dropped_below_target = vol < (target_upper_f - peak_tol)
        max_after_drop: float | None = None
        for row in projection_rows:
            qout = row.get(qout_key)
            qout_val = float(qout) if qout is not None else 0.0
            vol = vol + ((float(qin_value) - qout_val) * LS_TO_M3)
            if vol > max_vol:
                max_vol = vol
            if at_current_peak:
                if not dropped_below_target:
                    if vol < (target_upper_f - peak_tol):
                        dropped_below_target = True
                    continue
                if max_after_drop is None or vol > max_after_drop:
                    max_after_drop = vol
        if at_current_peak and dropped_below_target and max_after_drop is not None:
            return max_after_drop
        return max_vol

    if projected_peak_metric(0.0) > target_upper_f:
        return 0.0
    if projected_peak_metric(cap) <= target_upper_f:
        return cap

    lo, hi = 0.0, cap
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if projected_peak_metric(mid) <= target_upper_f:
            lo = mid
        else:
            hi = mid
    return lo


def align_next_15(dt: datetime) -> datetime:
    base = dt.replace(second=0, microsecond=0)
    remainder = base.minute % 15
    if remainder == 0:
        return base + timedelta(minutes=15)
    return base + timedelta(minutes=(15 - remainder))


def build_projection_rows(perfil: list[dict], start_dt: datetime, hours: int = 24) -> list[dict[str, Any]]:
    if not perfil:
        return []
    perfil_by_slot = {int(row["slot_index"]): row for row in perfil}
    start = align_next_15(start_dt)
    steps = max(1, int(hours * 4))
    out: list[dict[str, Any]] = []
    for i in range(steps):
        ts = start + timedelta(minutes=15 * i)
        slot_index = slot_index_from_datetime(ts)
        base = perfil_by_slot.get(slot_index, {})
        out.append(
            {
                "timestamp": ts.isoformat(timespec="minutes"),
                "slot_index": slot_index,
                "hora_texto": ts.strftime("%H:%M"),
                "qout_promedio": base.get("qout_promedio"),
                "qout_ia": base.get("qout_ia"),
            }
        )
    return out


def _ts_ms(ts: pd.Timestamp | datetime) -> int:
    if isinstance(ts, pd.Timestamp):
        return int(ts.timestamp() * 1000)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=CHILE_TZ)
    return int(ts.timestamp() * 1000)


def _ideal_for_timestamp(ts: datetime, ideal_by_slot: dict[int, float]) -> float | None:
    slot = slot_index_from_datetime(ts)
    return ideal_by_slot.get(slot)


def _ideal_band_bounds(ideal_y: float, pct: float = 0.10) -> tuple[float, float]:
    center = float(ideal_y)
    return center * (1.0 - pct), center * (1.0 + pct)


def _append_ideal_band_point(
    out: list[dict[str, float]],
    x_ms: int,
    ideal_y: float | None,
    pct: float = 0.10,
) -> None:
    if ideal_y is None:
        return
    lo, hi = _ideal_band_bounds(ideal_y, pct)
    out.append({"x": x_ms, "low": round(lo, 3), "high": round(hi, 3)})


def build_volume_overlay(
    point: str,
    df: pd.DataFrame,
    cfg: dict | None = None,
    *,
    qin_manual: float | None = None,
    qin_mode: str = "actual",
    projection_hours: int = 24,
) -> dict[str, Any] | None:
    """Genera curva ideal histórica y proyección forward para tags de volumen."""
    if df.empty or not is_volume_point(point):
        return None

    recinto = resolve_recinto(point, cfg)
    if not recinto:
        return None

    snapshot = _load_export(recinto)
    if not snapshot:
        return None

    cards = snapshot.get("cards") or snapshot.get("summary") or {}
    projection_block = snapshot.get("projection") or {}
    volumen_maximo = cards.get("volumen_maximo")
    vol_min = cards.get("volumen_banda_min")
    vol_max = cards.get("volumen_banda_max")
    if volumen_maximo and (vol_min is None or vol_max is None):
        vol_min = float(volumen_maximo) * 0.50
        vol_max = float(volumen_maximo) * 0.90

    last = df.iloc[-1]
    last_vol = float(last["value"])
    last_dt = last["time_local"].to_pydatetime().replace(tzinfo=None)
    profile_type = profile_group_for_day(last_dt)
    perfil = load_consumption_profile(recinto, profile_type)
    if not perfil:
        perfil = load_consumption_profile(recinto, "weekday")
    if not perfil:
        return None

    ideal_daily = build_sinusoidal_ideal_volume_series(perfil, "qout_ia", vol_min, vol_max)
    ideal_by_slot: dict[int, float] = {}
    for idx, row in enumerate(perfil):
        val = ideal_daily[idx] if idx < len(ideal_daily) else None
        if val is not None:
            ideal_by_slot[int(row["slot_index"])] = float(val)

    flow_tags = resolve_flow_tags(point, recinto, snapshot, cfg)
    qin_export = float(projection_block.get("qin_actual") or cards.get("last_qin") or 0.0)
    qin_export_fecha = cards.get("last_qin_fecha") or projection_block.get("start_dt")
    qin_live, qin_live_fecha = _fetch_live_qin(flow_tags.get("qin"), cfg)
    if qin_live is not None:
        qin_actual = qin_live
        qin_source = "rt3"
    else:
        qin_actual = qin_export
        qin_source = "export"
    qin_ideal = projection_block.get("qin_ideal")
    if qin_ideal is None:
        target_vol = vol_max if vol_max is not None else cards.get("target_vol")
        max_qin = cards.get("max_qin_historico")
        proj_rows = build_projection_rows(perfil, last_dt, hours=24)
        qin_ideal = compute_constrained_qin_ideal(last_vol, target_vol, proj_rows, "qout_ia", max_qin)
    qin_ideal_f = float(qin_ideal) if qin_ideal is not None else qin_actual

    mode = (qin_mode or "actual").strip().lower()
    if qin_manual is not None:
        qin_used = float(qin_manual)
        mode = "manual"
    elif mode == "ideal":
        qin_used = qin_ideal_f
    else:
        qin_used = qin_actual

    band_pct = float((cfg or {}).get("volume_band_pct", 0.10))

    ideal_series: list[dict[str, float]] = []
    ideal_band_series: list[dict[str, float]] = []
    ideal_projection_series: list[dict[str, float]] = []
    ideal_band_projection_series: list[dict[str, float]] = []
    for _, row in df.iterrows():
        ts = row["time_local"].to_pydatetime().replace(tzinfo=None)
        ideal_y = _ideal_for_timestamp(ts, ideal_by_slot)
        if ideal_y is not None:
            x_ms = _ts_ms(row["time_local"])
            ideal_series.append({"x": x_ms, "y": round(ideal_y, 3)})
            _append_ideal_band_point(ideal_band_series, x_ms, ideal_y, band_pct)

    proj_rows = build_projection_rows(perfil, last_dt, hours=projection_hours)
    proj_actual = simulate_volume_projection(last_vol, qin_used, proj_rows, "qout_ia")
    proj_ideal = simulate_volume_projection(last_vol, qin_ideal_f, proj_rows, "qout_ia")

    projection_series: list[dict[str, float]] = []
    projection_ideal_series: list[dict[str, float]] = []
    anchor_x = _ts_ms(last["time_local"])
    anchor_ideal_y = _ideal_for_timestamp(last_dt, ideal_by_slot)
    if anchor_ideal_y is not None:
        ideal_projection_series.append({"x": anchor_x, "y": round(anchor_ideal_y, 3)})
        _append_ideal_band_point(ideal_band_projection_series, anchor_x, anchor_ideal_y, band_pct)
    if ideal_series:
        projection_series.append({"x": anchor_x, "y": round(last_vol, 3)})
        projection_ideal_series.append({"x": anchor_x, "y": round(last_vol, 3)})

    for row, row_ideal in zip(proj_actual, proj_ideal):
        try:
            ts = datetime.fromisoformat(str(row["timestamp"]))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=CHILE_TZ)
        x_ms = _ts_ms(ts)
        projection_series.append({"x": x_ms, "y": round(float(row["volumen"]), 3)})
        projection_ideal_series.append({"x": x_ms, "y": round(float(row_ideal["volumen"]), 3)})
        ideal_y = _ideal_for_timestamp(ts.replace(tzinfo=None), ideal_by_slot)
        if ideal_y is not None:
            ideal_projection_series.append({"x": x_ms, "y": round(ideal_y, 3)})
            _append_ideal_band_point(ideal_band_projection_series, x_ms, ideal_y, band_pct)

    projection_steps: list[dict[str, float]] = []
    projection_detail: list[dict[str, Any]] = [
        {
            "kind": "anchor",
            "hora": last_dt.strftime("%H:%M"),
            "timestamp": last_dt.isoformat(timespec="minutes"),
            "x": anchor_x,
            "qin": round(qin_used, 3),
            "qout": None,
            "delta_vol": None,
            "volumen": round(last_vol, 3),
        }
    ]
    prev_vol = last_vol
    for row, proj_row in zip(proj_rows, proj_actual):
        try:
            ts = datetime.fromisoformat(str(row["timestamp"]))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=CHILE_TZ)
        qout = row.get("qout_ia")
        qout_val = round(float(qout), 4) if qout is not None else 0.0
        vol = round(float(proj_row["volumen"]), 3)
        delta_vol = round(vol - prev_vol, 3)
        x_ms = _ts_ms(ts)
        hora = row.get("hora_texto") or ts.strftime("%H:%M")
        projection_steps.append({
            "x": x_ms,
            "hora": hora,
            "qout_ia": qout_val,
        })
        projection_detail.append({
            "kind": "step",
            "hora": hora,
            "timestamp": ts.isoformat(timespec="minutes"),
            "x": x_ms,
            "qin": round(qin_used, 3),
            "qout": qout_val,
            "delta_vol": delta_vol,
            "volumen": vol,
        })
        prev_vol = vol

    return {
        "recinto": recinto,
        "meta": {
            "qin_actual": round(qin_actual, 3),
            "qin_ideal": round(qin_ideal_f, 3),
            "qin_used": round(qin_used, 3),
            "qin_mode": mode,
            "qin_point": flow_tags.get("qin"),
            "qin2_point": flow_tags.get("qin2"),
            "vol_point": flow_tags.get("vol") or point,
            "qout_point": flow_tags.get("qout"),
            "qout_label": _qout_label(flow_tags, snapshot),
            "qout_key": QOUT_KEY,
            "qin_source": qin_source,
            "qin_export": round(qin_export, 3) if qin_source == "rt3" and abs(qin_export - qin_actual) > 0.01 else None,
            "qin_export_fecha": qin_export_fecha if qin_source == "rt3" and abs(qin_export - qin_actual) > 0.01 else None,
            "qin_live_fecha": qin_live_fecha,
            "volumen_maximo": volumen_maximo,
            "volumen_banda_min": vol_min,
            "volumen_banda_max": vol_max,
            "volume_band_pct": band_pct,
            "target_vol": cards.get("target_vol") or vol_max,
            "last_volume": round(last_vol, 3),
            "export_updated_at": snapshot.get("generated_at"),
        },
        "ideal_series": ideal_series,
        "ideal_band_series": ideal_band_series,
        "ideal_projection_series": ideal_projection_series,
        "ideal_band_projection_series": ideal_band_projection_series,
        "projection_series": projection_series,
        "projection_ideal_series": projection_ideal_series,
        "projection_detail": projection_detail,
        "projection_model": {
            "anchor_x": anchor_x,
            "anchor_vol": round(last_vol, 3),
            "anchor_hora": last_dt.strftime("%H:%M"),
            "anchor_timestamp": last_dt.isoformat(timespec="minutes"),
            "qin_used": round(qin_used, 3),
            "steps": projection_steps,
            "ls_to_m3": LS_TO_M3,
        },
    }


def _volume_at_step(detail: list[dict[str, Any]], step_index: int) -> float | None:
    """step_index 0 = ancla; 1 = +15 min; 24 = +6 h; 96 = +24 h."""
    if not detail:
        return None
    idx = min(max(0, step_index), len(detail) - 1)
    vol = detail[idx].get("volumen")
    return round(float(vol), 3) if vol is not None else None


def summarize_volume_overlay(point: str, overlay: dict[str, Any]) -> dict[str, Any]:
    meta = overlay.get("meta") or {}
    detail = overlay.get("projection_detail") or []
    return {
        "point": point,
        "recinto": overlay.get("recinto"),
        "vol_point": meta.get("vol_point") or point,
        "qin_point": meta.get("qin_point"),
        "qin2_point": meta.get("qin2_point"),
        "qout_point": meta.get("qout_point"),
        "qout_label": meta.get("qout_label"),
        "volumen_actual": meta.get("last_volume"),
        "qin_used": meta.get("qin_used"),
        "qin_ideal": meta.get("qin_ideal"),
        "proy_6h": _volume_at_step(detail, 24),
        "proy_12h": _volume_at_step(detail, 48),
        "proy_24h": _volume_at_step(detail, 96),
        "volumen_maximo": meta.get("volumen_maximo"),
        "has_detail": bool(detail),
    }
