#!/usr/bin/env python3
"""Análisis GPU de caudal cp.pcp.huiliches — patrones nocturnos y pre-alarma de rotura."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from features import (
    build_features,
    build_trend_bands,
    label_pre_rupture,
    label_rupture_events,
)
from nocturnal import (
    daily_nocturnal_stats,
    detect_nocturnal_anomalies,
    global_nocturnal_summary,
    hourly_nocturnal_profile,
    night_mask,
)
from rupture_model import save_model, score_current, train_classifier
from rt3_client import fetch_series
from limits import compute_limits, daily_diurnal_stats
from anomaly_engine import analyze_series, evaluate_current
from sixsigma import build_sigma_bands, detect_patterns
from variable_profiles import attach_homolog, daily_reference_stats, evaluate_risk, get_profile

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"


def point_slug(point: str) -> str:
    return point.replace(".", "_").replace("/", "_")


def report_path_for(point: str, cfg: dict | None = None) -> Path:
    out_dir = BASE_DIR / (cfg or {}).get("model_dir", "output")
    return out_dir / "reports" / f"{point_slug(point)}.json"


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def prepare_dataset(cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    tz = cfg.get("timezone", "America/Santiago")
    df = fetch_series(cfg["point"], cfg["fini"], "*", cfg["ma"], tz=tz)
    if df.empty:
        raise ValueError(f"Sin datos para {cfg['point']}")

    night_start = cfg.get("night_start_hour", 22)
    night_end = cfg.get("night_end_hour", 6)

    noct_stats = daily_nocturnal_stats(df, night_start, night_end)
    noct_summary = global_nocturnal_summary(noct_stats)
    hourly_profile = hourly_nocturnal_profile(df, night_start, night_end)
    anomalies = detect_nocturnal_anomalies(noct_stats)

    df = build_trend_bands(
        df,
        use_dow=cfg.get("ll_hh_use_dow", True),
        warn_low=cfg.get("warn_low_pct", 10) / 100.0,
        warn_high=cfg.get("warn_high_pct", 90) / 100.0,
        alarm_low=cfg.get("alarm_low_pct", 2) / 100.0,
        alarm_high=cfg.get("alarm_high_pct", 98) / 100.0,
    )
    df = attach_homolog(df, cfg.get("ma", 15))
    var_profile = get_profile(cfg["point"], cfg.get("unit", "l/s"), cfg)
    df["date_local"] = df["time_local"].dt.date
    if var_profile.get("rotura_relevante", True):
        df["rupture"] = label_rupture_events(
            df,
            delta_umbral=cfg.get("delta_umbral", 3.0),
            win_steps=max(1, cfg.get("win_min", 15) // cfg.get("ma", 15)),
        )
        lookahead_steps = max(1, cfg.get("lookahead_min", 60) // cfg.get("ma", 15))
        df["pre_rupture"] = label_pre_rupture(df, lookahead_steps)
    else:
        df["rupture"] = False
        df["pre_rupture"] = False

    nmask = night_mask(df, night_start, night_end)
    df = build_features(df, nmask, noct_stats)
    df = analyze_series(df, cfg, var_profile)
    if "rotura_inmediata" in df.columns:
        df["rupture"] = df["rupture"] | df["rotura_inmediata"]
        if df["rotura_inmediata"].any():
            lookahead_steps = max(1, cfg.get("lookahead_min", 60) // cfg.get("ma", 15))
            pre_new = label_pre_rupture(df.assign(rupture=df["rotura_inmediata"]), lookahead_steps)
            df["pre_rupture"] = df["pre_rupture"] | pre_new
    df = df.dropna(subset=["value"]).reset_index(drop=True)

    def _records(frame: pd.DataFrame) -> list[dict]:
        if frame.empty:
            return []
        out = frame.copy()
        for col in out.columns:
            if out[col].dtype == object or str(out[col].dtype).startswith("date"):
                out[col] = out[col].astype(str)
        return out.to_dict(orient="records")

    nocturnal_report = {
        "summary": noct_summary,
        "profile_hourly": _records(hourly_profile),
        "anomalies": _records(anomalies),
        "recent_nights": _records(noct_stats.tail(7)),
    }
    return df, noct_stats, nocturnal_report


def run(cfg: dict, save: bool = True) -> dict:
    print(f"Descargando {cfg['point']} ({cfg['fini']}, MA={cfg['ma']})...")
    df, noct_stats, nocturnal_report = prepare_dataset(cfg)
    profile = get_profile(cfg["point"], cfg.get("unit", ""), cfg)
    cfg["unit"] = profile.get("unit") or cfg.get("unit", "")
    print(f"Tipo: {profile['label']} ({profile['type']}) · unidad {cfg['unit']}")

    night_start = cfg.get("night_start_hour", 22)
    night_end = cfg.get("night_end_hour", 6)
    diur_stats = daily_diurnal_stats(df, night_start, night_end)
    limits = compute_limits(df, noct_stats, diur_stats, cfg)

    n_ruptures = int(df["rupture"].sum())
    n_pre = int(df["pre_rupture"].sum())
    print(f"Serie: {len(df)} puntos | roturas: {n_ruptures} | pre-rotura: {n_pre}")

    summary = nocturnal_report["summary"]
    if summary:
        print(
            f"Nocturno — min global: {summary['min_nocturno_global']} | "
            f"max global: {summary['max_nocturno_global']}"
        )

    ln, ld, lg = limits["nocturno"], limits["diurno"], limits["global"]
    print(
        f"Límites — noct L={ln['l']} H={ln['h']} LL={ln['ll']} HH={ln['hh']} | "
        f"diur L={ld['l']} H={ld['h']} LL={ld['ll']} HH={ld['hh']} | "
        f"global L={lg['l']} H={lg['h']} LL={lg['ll']} HH={lg['hh']}"
    )

    last = df.iloc[-1]
    model = None
    metrics: dict = {}
    score: dict = {
        "prob": None,
        "estado": "ok",
        "nivel": 0,
        "caudal": round(float(last["value"]), 3),
        "ts": str(last["time_local"]),
    }
    if profile.get("rotura_relevante", True) and n_pre >= 5:
        print("Entrenando XGBoost GPU...")
        model, metrics = train_classifier(df)
        cols = metrics["features"]
        score = score_current(model, df, cols)
        print(f"Score actual: prob={score['prob']} nivel={score['nivel']} ({score['estado']})")
        if "roc_auc" in metrics:
            print(f"ROC-AUC test: {metrics['roc_auc']}")
        print("\nTop features:")
        for item in metrics.get("top_features", [])[:5]:
            print(f"  {item['feature']}: {item['importance']}")
    else:
        print("ML omitido — tipo sin rotura o muy pocos eventos pre-rotura")

    alarm = evaluate_current(df, cfg, profile)

    df_ss = build_sigma_bands(df, use_dow=cfg.get("ll_hh_use_dow", True))
    ss = detect_patterns(df_ss, cfg)
    risk = evaluate_risk(df, profile, cfg, alarm=alarm, sixsigma=ss, score=score)

    print(f"Valor: {score['caudal']} {cfg.get('unit', '')} @ {score['ts']}")
    if alarm["in_alarm"]:
        tag = "ROTURA INMEDIATA" if alarm.get("rotura_inmediata") else f"ALARMA {alarm['tipo']}"
        print(f"{tag}: {alarm['mensaje']}")
    print(f"Riesgo: nivel={risk['nivel']} ({risk['estado']}) — {risk['mensaje']}")
    h = risk.get("homolog") or {}
    if h.get("dev_pct_1d") is not None:
        print(f"Homólogo: vs ayer {h['dev_pct_1d']}% | vs semana {h.get('dev_pct_7d')}%")

    out_dir = BASE_DIR / cfg.get("model_dir", "output")
    model_path = out_dir / f"{point_slug(cfg['point'])}.json"
    if save and model is not None:
        save_model(model, metrics["features"], model_path)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "point": cfg["point"],
        "unit": cfg.get("unit", "l/s"),
        "fini": cfg.get("fini", "*-90d"),
        "ma": cfg.get("ma", 15),
        "variable_profile": profile,
        "data_points": len(df),
        "rupture_events": n_ruptures,
        "pre_rupture_samples": n_pre,
        "nocturnal": nocturnal_report,
        "limits": limits,
        "current_alarm": alarm,
        "risk_assessment": risk,
        "daily_stats": daily_reference_stats(df),
        "sixsigma_summary": ss.get("summary", {}),
        "model_metrics": {k: v for k, v in metrics.items() if k != "report"} if metrics else {},
        "classification_report": metrics.get("report") if metrics else None,
        "current_score": score,
        "model_path": str(model_path) if model is not None else None,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    report_json = json.dumps(report, indent=2, ensure_ascii=False)
    (out_dir / "last_report.json").write_text(report_json, encoding="utf-8")
    per_point = report_path_for(cfg["point"], cfg)
    per_point.parent.mkdir(parents=True, exist_ok=True)
    per_point.write_text(report_json, encoding="utf-8")
    print(f"\nReporte: {per_point}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Análisis GPU caudal Huiliches")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Ruta config.json")
    parser.add_argument("--point", help="Override point RT3")
    parser.add_argument("--fini", help="Override ventana (ej. *-90d)")
    parser.add_argument("--no-save", action="store_true", help="No guardar modelo")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    if args.point:
        cfg["point"] = args.point
    if args.fini:
        cfg["fini"] = args.fini

    try:
        run(cfg, save=not args.no_save)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
