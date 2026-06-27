#!/usr/bin/env python3
"""Portal web GPU Tag — análisis nocturno y pre-alarma de rotura."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests
from flask import Flask, jsonify, render_template, request

from anomaly_engine import analyze_series, series_point_payload
from analyze import DEFAULT_CONFIG, load_config, prepare_dataset, report_path_for, run
from limits import compute_limits, daily_diurnal_stats
from sixsigma import apply_sigma_lh_limits, build_sigma_bands, detect_patterns, pattern_markers
from variable_profiles import attach_homolog, daily_reference_stats, get_profile
from volume_projection import build_volume_overlay, is_volume_point, summarize_volume_overlay

BASE_DIR = Path(__file__).resolve().parent
RT3_HOST = os.environ.get("RT3_API_HOST", "http://rt3-d2:8090")
POINTS_URL = os.environ.get("RT3_POINTS_URL", f"{RT3_HOST}/api/points")
VALUE_URL = os.environ.get("RT3_VALUE_URL", f"{RT3_HOST}/api/point/{{tag}}/value")
TIMEOUT = int(os.environ.get("RT3_API_TIMEOUT", "120"))

app = Flask(__name__, static_folder="static", template_folder="templates")


def _base_cfg() -> dict:
    if DEFAULT_CONFIG.is_file():
        return load_config(DEFAULT_CONFIG)
    return {"point": "cp.pcp.huiliches", "unit": "l/s", "fini": "*-14d", "ma": 5, "model_dir": "output", "preset_points": []}


def _cfg_for_point(point: str, overrides: dict | None = None) -> dict:
    cfg = _base_cfg()
    cfg["point"] = point
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v is not None})
    var_over = (cfg.get("variable_overrides") or {}).get(point, {})
    profile = get_profile(point, var_over.get("unit") or "", cfg)
    cfg["unit"] = profile.get("unit") or cfg.get("unit", "")
    cfg["variable_type"] = profile.get("type")
    return cfg


def _load_report(point: str) -> dict | None:
    path = report_path_for(point, _base_cfg())
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    legacy = BASE_DIR / _base_cfg().get("model_dir", "output") / "last_report.json"
    if legacy.is_file():
        data = json.loads(legacy.read_text(encoding="utf-8"))
        if data.get("point") == point:
            return data
    return None


def _ts_ms(ts: pd.Timestamp) -> int:
    return int(ts.timestamp() * 1000)


def _downsample(df: pd.DataFrame, max_points: int = 2500) -> pd.DataFrame:
    if len(df) <= max_points:
        return df
    step = max(1, len(df) // max_points)
    return df.iloc[::step].reset_index(drop=True)


def _parse_lh_sigma(raw: str | None) -> int:
    try:
        n = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return n if n in (1, 2, 3) else 0


def _chart_payload(
    df: pd.DataFrame,
    unit: str,
    cfg: dict | None = None,
    *,
    qin_manual: float | None = None,
    qin_mode: str = "actual",
    lh_sigma: int = 0,
) -> dict[str, Any]:
    cfg = cfg or {}
    point = cfg.get("point", "")
    profile = get_profile(point, unit, cfg)
    unit = profile.get("unit") or unit
    if cfg.get("sixsigma_enabled", True):
        df = build_sigma_bands(df, use_dow=cfg.get("ll_hh_use_dow", True))
    if lh_sigma in (1, 2, 3):
        df = apply_sigma_lh_limits(df, lh_sigma)
    df = analyze_series(df, cfg, profile)
    if not df.empty and "homolog_1d" not in df.columns:
        df = attach_homolog(df, cfg.get("ma", 15))
    patterns = detect_patterns(df, cfg) if cfg.get("sixsigma_enabled", True) else {"events": [], "summary": {}}
    daily_stats = daily_reference_stats(df)

    work = _downsample(df)
    pct_cols = ("p05", "p20", "p50", "p80", "p95", "l", "h", "ll", "hh", "cl", "s1_lo", "s1_hi", "s2_lo", "s2_hi", "s3_lo", "s3_hi", "homolog_1d", "homolog_7d")
    series = []
    for _, row in work.iterrows():
        item: dict[str, Any] = {"x": _ts_ms(row["time_local"]), "y": round(float(row["value"]), 3)}
        for col in pct_cols:
            if col in work.columns and pd.notna(row[col]):
                item[col] = round(float(row[col]), 3)
        item.update(series_point_payload(row))
        series.append(item)
    rupt_mask = df["rotura_inmediata"] if "rotura_inmediata" in df.columns else df["rupture"]
    ruptures = [{"x": _ts_ms(r.time_local), "y": round(float(r.value), 3)} for r in df.loc[rupt_mask].itertuples()]
    pre = [{"x": _ts_ms(r.time_local), "y": round(float(r.value), 3)} for r in df.loc[df["pre_rupture"] & ~rupt_mask].itertuples()]
    payload: dict[str, Any] = {
        "unit": unit,
        "count": len(df),
        "series": series,
        "ruptures": ruptures,
        "pre_ruptures": pre,
        "sixsigma": {
            "summary": patterns.get("summary", {}),
            "markers": pattern_markers(patterns),
            "recent": patterns.get("events", [])[-12:],
        },
        "variable_profile": profile,
        "daily_stats": daily_stats,
        "lh_sigma": lh_sigma,
    }
    if is_volume_point(point):
        overlay = build_volume_overlay(point, df, cfg, qin_manual=qin_manual, qin_mode=qin_mode)
        if overlay:
            payload["volume_projection"] = overlay
    return payload


@app.route("/")
def index():
    cfg = _base_cfg()
    initial_point = request.args.get("point") or cfg.get("point", "cp.pcp.huiliches")
    return render_template("index.html", initial_point=initial_point, default_unit=cfg.get("unit", "l/s"))


@app.route("/api/config")
def api_config():
    cfg = _base_cfg()
    return jsonify({"point": cfg.get("point"), "unit": cfg.get("unit", "l/s"), "fini": cfg.get("fini", "*-14d"), "ma": cfg.get("ma", 5), "preset_points": cfg.get("preset_points", [])})


@app.route("/api/points")
def api_points():
    q = (request.args.get("q") or "").strip().lower()
    only_caudal = request.args.get("caudal", "0") in ("1", "true", "yes")
    try:
        r = requests.get(POINTS_URL, timeout=TIMEOUT)
        r.raise_for_status()
        points = r.json().get("points", [])
        presets = set(_base_cfg().get("preset_points", []))
        out = []
        for p in points:
            tag = p.get("tag") or p.get("point") or ""
            if not tag:
                continue
            nombre = p.get("nombre") or p.get("descripcion") or ""
            label = " — ".join(x for x in [tag, nombre] if x)
            if only_caudal and "caudal" not in label.lower() and tag not in presets:
                continue
            if q and q not in label.lower():
                continue
            out.append({"tag": tag, "nombre": nombre, "label": label, "preset": tag in presets})
        out.sort(key=lambda x: (not x["preset"], x["tag"]))
        limit = int(request.args.get("limit", "0"))
        return jsonify({"count": len(out[:limit]), "total": len(out), "points": out[:limit] if limit > 0 else out})
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/reports")
def api_reports():
    out_dir = BASE_DIR / _base_cfg().get("model_dir", "output") / "reports"
    rows = []
    if out_dir.is_dir():
        for path in sorted(out_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                score = data.get("current_score") or {}
                rows.append({"point": data.get("point"), "generated_at": data.get("generated_at"), "estado": score.get("estado"), "nivel": score.get("nivel"), "prob": score.get("prob"), "caudal": score.get("caudal")})
            except (json.JSONDecodeError, OSError):
                continue
    return jsonify({"count": len(rows), "reports": rows})


@app.route("/api/report")
def api_report():
    point = (request.args.get("point") or "").strip()
    if not point:
        return jsonify({"error": "missing point"}), 400
    report = _load_report(point)
    if not report:
        return jsonify({"point": point, "report": None})
    return jsonify({"point": point, "report": report})


def _empty_chart_payload(point: str, cfg: dict, message: str) -> dict[str, Any]:
    profile = get_profile(point, "", cfg)
    unit = profile.get("unit") or cfg.get("unit", "")
    return {
        "point": point,
        "empty": True,
        "message": message,
        "unit": unit,
        "count": 0,
        "series": [],
        "ruptures": [],
        "pre_ruptures": [],
        "sixsigma": {"summary": {}, "markers": {}, "recent": []},
        "variable_profile": profile,
        "daily_stats": [],
    }


@app.route("/api/chart")
def api_chart():
    point = (request.args.get("point") or "").strip()
    if not point:
        return jsonify({"error": "missing point"}), 400
    fini = request.args.get("fini") or _base_cfg().get("fini", "*-14d")
    ma = int(request.args.get("ma", str(_base_cfg().get("ma", 5))))
    qin_mode = request.args.get("qin_mode") or "actual"
    qin_raw = request.args.get("qin")
    qin_manual = float(qin_raw) if qin_raw not in (None, "") else None
    lh_sigma = _parse_lh_sigma(request.args.get("lh_sigma"))
    try:
        cfg = _cfg_for_point(point, {"fini": fini, "ma": ma, "point": point})
        df, _, _ = prepare_dataset(cfg)
        return jsonify({
            "point": point,
            **_chart_payload(
                df,
                cfg.get("unit", "l/s"),
                cfg,
                qin_manual=qin_manual,
                qin_mode=qin_mode,
                lh_sigma=lh_sigma,
            ),
        })
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("Sin datos"):
            cfg = _cfg_for_point(point, {"fini": fini, "ma": ma, "point": point})
            hint = f"{msg} — prueba ampliar el rango (30/90 días)."
            return jsonify(_empty_chart_payload(point, cfg, hint))
        return jsonify({"error": msg}), 400
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/volume/tanks")
def api_volume_tanks():
    """Resumen de proyección de volumen para todos los estanques configurados."""
    qin_mode = request.args.get("qin_mode") or "actual"
    qin_raw = request.args.get("qin")
    qin_manual = float(qin_raw) if qin_raw not in (None, "") else None
    current_point = (request.args.get("point") or "").strip()
    cfg_base = _base_cfg()
    volume_map = cfg_base.get("volume_recintos") or {}
    tanks: list[dict[str, Any]] = []
    for vol_point, _recinto in volume_map.items():
        try:
            cfg = _cfg_for_point(vol_point, {"fini": "*-6h", "ma": 15, "point": vol_point})
            df, _, _ = prepare_dataset(cfg)
            overlay = build_volume_overlay(
                vol_point,
                df,
                cfg,
                qin_manual=qin_manual if vol_point == current_point else None,
                qin_mode=qin_mode,
            )
            if not overlay:
                tanks.append({"point": vol_point, "recinto": _recinto, "error": "sin perfil demo_ia"})
                continue
            summary = summarize_volume_overlay(vol_point, overlay)
            summary["current"] = vol_point == current_point
            if vol_point == current_point:
                summary["projection_detail"] = overlay.get("projection_detail") or []
                summary["meta"] = overlay.get("meta") or {}
            tanks.append(summary)
        except ValueError as exc:
            tanks.append({"point": vol_point, "recinto": _recinto, "error": str(exc)})
        except requests.RequestException as exc:
            return jsonify({"error": str(exc)}), 502
    return jsonify({"count": len(tanks), "tanks": tanks})


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    body = request.get_json(silent=True) or {}
    point = (body.get("point") or "").strip()
    if not point:
        return jsonify({"error": "missing point"}), 400
    cfg = _cfg_for_point(point, {"fini": body.get("fini") or _base_cfg().get("fini", "*-14d"), "ma": int(body.get("ma") or _base_cfg().get("ma", 15))})
    try:
        report = run(cfg, save=body.get("save", True))
        return jsonify({"ok": True, "report": report})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "gpu_tag", "port": int(os.environ.get("PORT", "5092")), "time": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5092"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1", use_reloader=False)
