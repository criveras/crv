#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rt3-ia Flask app

Servicio simple para:
- Registrar recintos/estanques (ej: tk_copa) con sus 3 tags: qin, vol, qout
- Leer datos históricos desde MySQL legacy (rtdata.tag_{tagid})
- Calcular promedios cada 15 minutos, interpolando cuando no existan datos
- Guardar los resultados en un SQLite por recinto: <nombre_recinto>.sqlite
  con tabla: medidas(fecha, qin, vol, qout)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import math
import re
import statistics
import csv
import json
from functools import lru_cache
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import mysql.connector
from flask import Flask, jsonify, redirect, render_template_string, request, url_for
try:
    import redis
except Exception:
    redis = None

# Asegurar que el directorio raíz del proyecto esté en sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent  # /home/criveras/app
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
RT3_SUIT_LIB_DIR = ROOT_DIR / "rt3-suit" / "lib"
if str(RT3_SUIT_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(RT3_SUIT_LIB_DIR))

from legacy_shared_config import load_legacy_config
from config_influxdb import INFLUX_BUCKET, INFLUX_ORG, INFLUX_TOKEN, INFLUX_URL


RT3_IA_DIR = ROOT_DIR / "rt3-ia"
RT3_DATA_DIR = ROOT_DIR / "rt3-data"
RT3_IA_DATA_DIR = RT3_IA_DIR  # se usarán los .sqlite dentro de rt3-ia
RT3_IA_EXPORT_DIR = RT3_IA_DIR / "exports"
RT3_DB_PATH = RT3_DATA_DIR / "rt3.sqlite3"

RT3_IA_CONFIG_DB = RT3_IA_DIR / "rt3_ia_config.sqlite"
RT3_IA_CORR_DB = RT3_IA_DIR / "rt3_ia_correlacion.sqlite"


# ---------------------------------------------------------------------------
# Configuración MySQL legacy
# ---------------------------------------------------------------------------

_legacy_cfg = load_legacy_config()
_mysql_cfg = _legacy_cfg.get("mysql_legacy", {}) or {}

MYSQL_CONFIG = {
    "host": _mysql_cfg.get("host", "100.94.219.59"),
    "port": int(_mysql_cfg.get("port", 3366) or 3366),
    "user": _mysql_cfg.get("user", "root"),
    "password": _mysql_cfg.get("password", "250877"),
    "database": "rtdata",  # para leer rtdata.tag_{id}
}


def get_mysql_connection():
    return mysql.connector.connect(**MYSQL_CONFIG)


def get_mysql_struct_connection():
    cfg = dict(MYSQL_CONFIG)
    cfg["database"] = "rtstruct"
    return mysql.connector.connect(**cfg)


CHILE_TZ = ZoneInfo("America/Santiago")
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1").strip()
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("REDIS_DB", "0"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "").strip() or None
REDIS_HASH_POINT = os.environ.get("REDIS_HASH_POINT", "HT_POINT").strip() or "HT_POINT"
REDIS_HASH_POINT_RAW = os.environ.get("REDIS_HASH_POINT_RAW", "HT_POINT_RAW").strip() or "HT_POINT_RAW"
REDIS_HASH_POINT_TS = os.environ.get("REDIS_HASH_POINT_TS", "HT_POINT_TS").strip() or "HT_POINT_TS"


# ---------------------------------------------------------------------------
# Modelo de datos
# ---------------------------------------------------------------------------


@dataclass
class RecintoConfig:
    nombre: str
    tag_qin1: Optional[int]
    tag_vol: Optional[int]
    tag_qout: Optional[int]
    # Unidad interna:
    # - "l/s": se guarda como l/s (sin conversión)
    # - "m3/hr": se convierte a l/s dividiendo por 3.6 y se guarda como qout en l/s
    qout_unit: str = "l/s"
    tag_qin2: Optional[int] = None
    point_qin1: Optional[str] = None
    point_qin2: Optional[str] = None
    point_vol: Optional[str] = None
    point_qout: Optional[str] = None
    source_type: str = "influxdb"
    volumen_maximo: Optional[float] = None
    activo: bool = True
    last_run_at: Optional[str] = None  # ISO string
    last_rows_saved: Optional[int] = None


def ensure_dirs():
    RT3_IA_DIR.mkdir(parents=True, exist_ok=True)
    RT3_IA_EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def init_config_db():
    """SQLite con tabla de recintos configurados por usuario."""
    ensure_dirs()
    conn = sqlite3.connect(RT3_IA_CONFIG_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS recintos (
                nombre TEXT PRIMARY KEY,
                tag_qin INTEGER NOT NULL,
                tag_qin1 INTEGER,
                tag_qin2 INTEGER,
                tag_vol INTEGER NOT NULL,
                tag_qout INTEGER NOT NULL,
                point_qin1 TEXT,
                point_qin2 TEXT,
                point_vol TEXT,
                point_qout TEXT,
                source_type TEXT NOT NULL DEFAULT 'influxdb',
                volumen_maximo REAL,
                activo INTEGER NOT NULL DEFAULT 1,
                qout_unit TEXT NOT NULL DEFAULT 'l/s',
                last_run_at TEXT,
                last_rows_saved INTEGER
            )
            """
        )
        # Intentar agregar columnas si la tabla es antigua
        cur.execute("PRAGMA table_info(recintos)")
        cols = [row[1] for row in cur.fetchall()]
        if "activo" not in cols:
            cur.execute("ALTER TABLE recintos ADD COLUMN activo INTEGER NOT NULL DEFAULT 1")
        if "tag_qin1" not in cols:
            cur.execute("ALTER TABLE recintos ADD COLUMN tag_qin1 INTEGER")
        if "tag_qin2" not in cols:
            cur.execute("ALTER TABLE recintos ADD COLUMN tag_qin2 INTEGER")
        if "qout_unit" not in cols:
            cur.execute("ALTER TABLE recintos ADD COLUMN qout_unit TEXT NOT NULL DEFAULT 'l/s'")
        if "point_qin1" not in cols:
            cur.execute("ALTER TABLE recintos ADD COLUMN point_qin1 TEXT")
        if "point_qin2" not in cols:
            cur.execute("ALTER TABLE recintos ADD COLUMN point_qin2 TEXT")
        if "point_vol" not in cols:
            cur.execute("ALTER TABLE recintos ADD COLUMN point_vol TEXT")
        if "point_qout" not in cols:
            cur.execute("ALTER TABLE recintos ADD COLUMN point_qout TEXT")
        if "source_type" not in cols:
            cur.execute("ALTER TABLE recintos ADD COLUMN source_type TEXT NOT NULL DEFAULT 'influxdb'")
        if "volumen_maximo" not in cols:
            cur.execute("ALTER TABLE recintos ADD COLUMN volumen_maximo REAL")
        if "last_run_at" not in cols:
            cur.execute("ALTER TABLE recintos ADD COLUMN last_run_at TEXT")
        if "last_rows_saved" not in cols:
            cur.execute("ALTER TABLE recintos ADD COLUMN last_rows_saved INTEGER")

        # Migración suave:
        # si existe la columna vieja tag_qin pero aún no hay tag_qin1, llenamos tag_qin1 con tag_qin.
        try:
            cur.execute(
                "UPDATE recintos SET tag_qin1 = COALESCE(tag_qin1, tag_qin) WHERE tag_qin1 IS NULL OR tag_qin1 = 0"
            )
        except sqlite3.OperationalError:
            # si no existe tag_qin (tabla nueva), ignorar
            pass
        conn.commit()
    finally:
        conn.close()


def init_corr_db():
    ensure_dirs()
    conn = sqlite3.connect(RT3_IA_CORR_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS corr_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL UNIQUE,
                fecha_ini TEXT NOT NULL,
                fecha_fin TEXT NOT NULL,
                sqlite_file TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS corr_project_tags (
                project_id INTEGER NOT NULL,
                tagid INTEGER NOT NULL,
                codigo_tag TEXT NOT NULL,
                medidor_tipo TEXT NOT NULL,
                pos INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (project_id, tagid),
                FOREIGN KEY(project_id) REFERENCES corr_projects(id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _safe_corr_name(nombre: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", (nombre or "").strip())
    return safe.strip("_") or "corr_project"


def _corr_sqlite_path(nombre: str) -> Path:
    return RT3_IA_DIR / f"corr_{_safe_corr_name(nombre)}.sqlite"


def _default_corr_results_dir(nombre: Optional[str] = None) -> Path:
    if nombre:
        return RT3_IA_DIR / f"out_corr1_{_safe_corr_name(nombre)}"
    return RT3_IA_DIR / "out_corr1"


def load_corr1_outputs(outdir: Path) -> Dict[str, object]:
    result: Dict[str, object] = {
        "outdir": str(outdir),
        "exists": outdir.exists(),
        "metrics": None,
        "forecast_rows": [],
        "top_corr_rows": [],
        "matrix_headers": [],
        "matrix_rows": [],
        "focus_driver": "cp_escorial_entrada_caudal",
        "focus_driver_rows": [],
        "focus_driver_impact_rows": [],
        "insights": [],
        "errors": [],
        "files": {
            "metrics_json": str(outdir / "metrics.json"),
            "forecast_csv": str(outdir / "forecast.csv"),
            "correlation_matrix_csv": str(outdir / "correlation_matrix.csv"),
            "model_pt": str(outdir / "model.pt"),
        },
    }
    if not outdir.exists():
        result["errors"].append(f"No existe el directorio de resultados: {outdir}")
        return result

    metrics_path = outdir / "metrics.json"
    forecast_path = outdir / "forecast.csv"
    corr_path = outdir / "correlation_matrix.csv"

    metrics_payload: Optional[Dict[str, object]] = None
    if metrics_path.exists():
        try:
            with open(metrics_path, "r", encoding="utf-8") as f:
                metrics_payload = json.load(f)
            result["metrics"] = metrics_payload
        except Exception as exc:
            result["errors"].append(f"No se pudo leer metrics.json: {exc}")
    else:
        result["errors"].append("Falta metrics.json")

    if forecast_path.exists():
        try:
            with open(forecast_path, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            result["forecast_rows"] = rows[:96]
        except Exception as exc:
            result["errors"].append(f"No se pudo leer forecast.csv: {exc}")
    else:
        result["errors"].append("Falta forecast.csv")

    target = None
    if metrics_payload:
        target = str(metrics_payload.get("target") or "").strip() or None
        top_corr = metrics_payload.get("top_corr_with_target")
        if isinstance(top_corr, dict):
            result["top_corr_rows"] = [
                {"tag": str(k), "corr": float(v) if v is not None else None}
                for k, v in list(top_corr.items())[:12]
            ]

    if corr_path.exists():
        try:
            with open(corr_path, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.reader(f))
            if rows:
                headers = rows[0][1:]
                result["matrix_headers"] = headers
                matrix_rows = []
                for row in rows[1:]:
                    if not row:
                        continue
                    row_name = row[0]
                    values = []
                    for idx, raw in enumerate(row[1:]):
                        tag = headers[idx] if idx < len(headers) else f"col_{idx}"
                        try:
                            corr_value = float(raw)
                        except Exception:
                            corr_value = None
                        values.append({"tag": tag, "corr": corr_value})
                    matrix_rows.append({"tag": row_name, "values": values})
                    if row_name != target:
                        continue
                    if not result["top_corr_rows"]:
                        pairs = []
                        for item in values:
                            if item["corr"] is None:
                                continue
                            pairs.append({"tag": item["tag"], "corr": item["corr"]})
                        pairs.sort(key=lambda item: abs(float(item["corr"])), reverse=True)
                        result["top_corr_rows"] = pairs[:12]
                result["matrix_rows"] = matrix_rows
        except Exception as exc:
            result["errors"].append(f"No se pudo leer correlation_matrix.csv: {exc}")
    else:
        result["errors"].append("Falta correlation_matrix.csv")

    if result["matrix_rows"]:
        focus_driver = str(result.get("focus_driver") or "").strip()
        for row in result["matrix_rows"]:
            if str(row["tag"]) != focus_driver:
                continue
            pairs = []
            for item in row["values"]:
                corr_value = item.get("corr")
                if corr_value is None:
                    continue
                pairs.append({"tag": item["tag"], "corr": corr_value})
            pairs.sort(key=lambda item: abs(float(item["corr"])), reverse=True)
            result["focus_driver_rows"] = pairs
            break

    if result["focus_driver_rows"]:
        impact_rows = []
        focus_driver = str(result.get("focus_driver") or "").strip()
        for item in result["focus_driver_rows"]:
            tag = str(item.get("tag") or "")
            corr_value = item.get("corr")
            if corr_value is None or tag == focus_driver:
                continue
            abs_corr = abs(float(corr_value))
            if abs_corr >= 0.8:
                impact_level = "alto"
                impact_hint = "muy relacionado"
            elif abs_corr >= 0.5:
                impact_level = "medio"
                impact_hint = "relacion apreciable"
            elif abs_corr >= 0.2:
                impact_level = "bajo"
                impact_hint = "senal debil"
            else:
                impact_level = "muy bajo"
                impact_hint = "casi independiente"
            direction = "sube" if float(corr_value) > 0 else ("baja" if float(corr_value) < 0 else "neutro")
            impact_rows.append(
                {
                    "tag": tag,
                    "corr": float(corr_value),
                    "impact_level": impact_level,
                    "impact_hint": impact_hint,
                    "direction": direction,
                }
            )
        result["focus_driver_impact_rows"] = impact_rows

    if result["matrix_rows"]:
        pair_scores = []
        seen_pairs = set()
        for row in result["matrix_rows"]:
            row_tag = str(row["tag"])
            for item in row["values"]:
                col_tag = str(item.get("tag") or "")
                corr_value = item.get("corr")
                if corr_value is None or row_tag == col_tag:
                    continue
                pair_key = tuple(sorted((row_tag, col_tag)))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                pair_scores.append({"a": pair_key[0], "b": pair_key[1], "corr": float(corr_value)})
        pair_scores.sort(key=lambda item: abs(item["corr"]), reverse=True)
        if pair_scores:
            strongest = pair_scores[0]
            direction = "se mueven juntos" if strongest["corr"] > 0 else "se mueven en sentido contrario"
            result["insights"].append(
                f"Relacion mas fuerte del sistema: {strongest['a']} y {strongest['b']} ({strongest['corr']:.3f}); {direction}."
            )
        negative_pairs = [p for p in pair_scores if p["corr"] < 0]
        if negative_pairs:
            strongest_neg = negative_pairs[0]
            result["insights"].append(
                f"Relacion inversa mas marcada: {strongest_neg['a']} vs {strongest_neg['b']} ({strongest_neg['corr']:.3f})."
            )
    if result["focus_driver_impact_rows"]:
        top_impacts = result["focus_driver_impact_rows"][:3]
        tags = ", ".join([f"{row['tag']} ({row['corr']:.3f})" for row in top_impacts])
        result["insights"].append(
            f"Las variables mas sensibles a {result['focus_driver']} son: {tags}."
        )
        weak_count = sum(1 for row in result["focus_driver_impact_rows"] if abs(float(row["corr"])) < 0.2)
        if weak_count >= max(1, len(result["focus_driver_impact_rows"]) // 2):
            result["insights"].append(
                f"{result['focus_driver']} muestra relacion debil con la mayor parte del sistema; no aparece como palanca fuerte en estos datos."
            )

    return result


def build_corr1_comparison(result_a: Dict[str, object], result_b: Dict[str, object]) -> Dict[str, object]:
    def _label(result: Dict[str, object], fallback: str) -> str:
        metrics = result.get("metrics") or {}
        model = str(metrics.get("model") or "").strip()
        target = str(metrics.get("target") or "").strip()
        if model and target:
            return f"{model} | {target}"
        if model:
            return model
        return fallback

    compare: Dict[str, object] = {
        "label_a": _label(result_a, "resultado A"),
        "label_b": _label(result_b, "resultado B"),
        "metric_rows": [],
        "forecast_rows": [],
        "winner_summary": [],
    }

    metrics_a = (result_a.get("metrics") or {}).get("metrics") or {}
    metrics_b = (result_b.get("metrics") or {}).get("metrics") or {}
    metric_names = ["train_mse", "val_mse", "test_mse", "train_mae", "val_mae", "test_mae"]
    for name in metric_names:
        a = metrics_a.get(name)
        b = metrics_b.get(name)
        winner = "-"
        if a is not None and b is not None:
            if float(a) < float(b):
                winner = "A"
            elif float(b) < float(a):
                winner = "B"
            else:
                winner = "empate"
        compare["metric_rows"].append({"name": name, "a": a, "b": b, "winner": winner})

    wins_a = sum(1 for row in compare["metric_rows"] if row["winner"] == "A")
    wins_b = sum(1 for row in compare["metric_rows"] if row["winner"] == "B")
    if wins_a > wins_b:
        compare["winner_summary"].append(f"Mejor global: {compare['label_a']} ({wins_a} metricas ganadas)")
    elif wins_b > wins_a:
        compare["winner_summary"].append(f"Mejor global: {compare['label_b']} ({wins_b} metricas ganadas)")
    elif wins_a == wins_b and wins_a > 0:
        compare["winner_summary"].append("Comparacion equilibrada: ambos modelos ganan en metricas distintas")

    forecast_a = result_a.get("forecast_rows") or []
    forecast_b = result_b.get("forecast_rows") or []
    if forecast_a and forecast_b:
        col_a = next((k for k in forecast_a[0].keys() if k != "fecha"), None)
        col_b = next((k for k in forecast_b[0].keys() if k != "fecha"), None)
        by_fecha_b = {str(row.get("fecha")): row for row in forecast_b}
        for row_a in forecast_a[:24]:
            fecha = str(row_a.get("fecha"))
            row_b = by_fecha_b.get(fecha)
            if not row_b:
                continue
            a_val = row_a.get(col_a) if col_a else None
            b_val = row_b.get(col_b) if col_b else None
            try:
                delta = float(b_val) - float(a_val)
            except Exception:
                delta = None
            compare["forecast_rows"].append(
                {
                    "fecha": fecha,
                    "a": a_val,
                    "b": b_val,
                    "delta": delta,
                }
            )

    return compare


def load_corr_timeseries_analysis(
    sqlite_path: Optional[str],
    selected_columns: List[str],
    limit: int = 192,
    corr_window: int = 12,
) -> Dict[str, object]:
    analysis: Dict[str, object] = {
        "available_columns": [],
        "selected_columns": [],
        "labels": [],
        "datasets": [],
        "markers": [],
        "corr_windows": [],
        "insights": [],
    }
    if not sqlite_path:
        return analysis
    path = Path(str(sqlite_path))
    if not path.exists():
        return analysis

    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(correlation_data)")
        available_columns = [str(row[1]) for row in cur.fetchall() if str(row[1]) != "fecha"]
        analysis["available_columns"] = available_columns
        selected = [col for col in selected_columns if col in available_columns][:3]
        if not selected:
            selected = available_columns[:3]
        analysis["selected_columns"] = selected
        if not selected:
            return analysis
        quoted = ", ".join([f'"{col}"' for col in selected])
        query = f"SELECT fecha, {quoted} FROM correlation_data ORDER BY fecha DESC LIMIT ?"
        cur.execute(query, (int(limit),))
        rows = list(reversed(cur.fetchall()))
    finally:
        conn.close()

    if not rows:
        return analysis

    labels = [str(row[0]) for row in rows]
    analysis["labels"] = labels
    colors = ["#0f6cbd", "#ef6c00", "#2e7d32"]
    value_map: Dict[str, List[Optional[float]]] = {col: [] for col in selected}
    norm_map: Dict[str, List[Optional[float]]] = {col: [] for col in selected}

    for idx, col in enumerate(selected):
        values: List[Optional[float]] = []
        for row in rows:
            raw = row[idx + 1]
            try:
                values.append(float(raw) if raw is not None else None)
            except Exception:
                values.append(None)
        clean = [v for v in values if v is not None]
        if clean:
            mean_v = statistics.fmean(clean)
            std_v = statistics.pstdev(clean) if len(clean) > 1 else 1.0
            if std_v < 1e-8:
                std_v = 1.0
        else:
            mean_v = 0.0
            std_v = 1.0
        normalized = [((v - mean_v) / std_v) if v is not None else None for v in values]
        value_map[col] = values
        norm_map[col] = normalized
        analysis["datasets"].append({"name": col, "color": colors[idx % len(colors)], "values": normalized})

        anomaly_candidates = []
        for point_idx, z in enumerate(normalized):
            if z is None or abs(z) < 2.3:
                continue
            anomaly_candidates.append({"index": point_idx, "value": z})
        anomaly_candidates.sort(key=lambda item: abs(item["value"]), reverse=True)
        for marker in anomaly_candidates[:4]:
            analysis["markers"].append(
                {
                    "series": col,
                    "index": marker["index"],
                    "zscore": marker["value"],
                    "shape": "circle",
                }
            )

    if len(selected) >= 2 and len(labels) >= max(4, corr_window):
        a_vals = value_map[selected[0]]
        b_vals = value_map[selected[1]]
        corr_hits = []
        for end in range(corr_window - 1, len(labels)):
            start = end - corr_window + 1
            win_a = [a_vals[i] for i in range(start, end + 1) if a_vals[i] is not None and b_vals[i] is not None]
            win_b = [b_vals[i] for i in range(start, end + 1) if a_vals[i] is not None and b_vals[i] is not None]
            if len(win_a) < max(4, corr_window // 2):
                continue
            try:
                corr = statistics.correlation(win_a, win_b)
            except Exception:
                continue
            if abs(corr) >= 0.7:
                corr_hits.append({"start": start, "end": end, "corr": corr})
        merged = []
        for hit in corr_hits:
            if not merged or hit["start"] > merged[-1]["end"] + 1:
                merged.append(hit.copy())
            else:
                merged[-1]["end"] = hit["end"]
                if abs(hit["corr"]) > abs(merged[-1]["corr"]):
                    merged[-1]["corr"] = hit["corr"]
        analysis["corr_windows"] = merged[:8]
        if merged:
            strongest = max(merged, key=lambda item: abs(item["corr"]))
            analysis["insights"].append(
                f"En varias ventanas temporales, {selected[0]} y {selected[1]} muestran correlacion {'positiva' if strongest['corr'] > 0 else 'inversa'} fuerte (hasta {strongest['corr']:.3f})."
            )
    if analysis["markers"]:
        markers_by_series: Dict[str, int] = {}
        for marker in analysis["markers"]:
            markers_by_series[marker["series"]] = markers_by_series.get(marker["series"], 0) + 1
        hottest = max(markers_by_series.items(), key=lambda item: item[1])
        analysis["insights"].append(
            f"La serie con mas anomalias visibles en este tramo es {hottest[0]} ({hottest[1]} marcas)."
        )

    return analysis


def list_corr_projects() -> List[Dict[str, object]]:
    init_corr_db()
    conn = sqlite3.connect(RT3_IA_CORR_DB)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT id, nombre, fecha_ini, fecha_fin, sqlite_file, created_at, updated_at FROM corr_projects ORDER BY updated_at DESC"
        )
        rows = cur.fetchall()
        out: List[Dict[str, object]] = []
        for row in rows:
            cur.execute(
                "SELECT tagid, codigo_tag, medidor_tipo, pos FROM corr_project_tags WHERE project_id = ? ORDER BY pos ASC, codigo_tag ASC",
                (int(row["id"]),),
            )
            tags = [
                {
                    "tagid": int(r[0]),
                    "codigo_tag": str(r[1]),
                    "medidor_tipo": str(r[2]),
                    "pos": int(r[3]),
                }
                for r in cur.fetchall()
            ]
            out.append(
                {
                    "id": int(row["id"]),
                    "nombre": str(row["nombre"]),
                    "fecha_ini": str(row["fecha_ini"]),
                    "fecha_fin": str(row["fecha_fin"]),
                    "sqlite_file": str(row["sqlite_file"]),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                    "tags": tags,
                }
            )
        return out
    finally:
        conn.close()


def get_mysql_legacy_tag_catalog(limit: int = 5000) -> List[Dict[str, object]]:
    conn = get_mysql_struct_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, codigo_tag FROM rtstruct.tag ORDER BY id ASC LIMIT %s", (int(limit),))
        return [{"id": int(r[0]), "codigo_tag": str(r[1])} for r in cur.fetchall()]
    finally:
        conn.close()


def get_mysql_legacy_codigo_tag_by_id(tagid: int) -> Optional[str]:
    conn = get_mysql_struct_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT codigo_tag FROM rtstruct.tag WHERE id = %s LIMIT 1", (int(tagid),))
        row = cur.fetchone()
        if not row:
            return None
        return str(row[0]) if row[0] is not None else None
    finally:
        conn.close()


def save_corr_project(
    nombre: str,
    fecha_ini: datetime,
    fecha_fin: datetime,
    tags: List[Dict[str, object]],
) -> None:
    init_corr_db()
    if not nombre.strip():
        raise ValueError("Nombre de proyecto requerido")
    if not tags:
        raise ValueError("Debes agregar al menos un tag")
    sqlite_file = str(_corr_sqlite_path(nombre))
    now = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(RT3_IA_CORR_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO corr_projects (nombre, fecha_ini, fecha_fin, sqlite_file, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(nombre) DO UPDATE SET
              fecha_ini=excluded.fecha_ini,
              fecha_fin=excluded.fecha_fin,
              sqlite_file=excluded.sqlite_file,
              updated_at=excluded.updated_at
            """,
            (
                nombre.strip(),
                fecha_ini.isoformat(timespec="minutes"),
                fecha_fin.isoformat(timespec="minutes"),
                sqlite_file,
                now,
                now,
            ),
        )
        cur.execute("SELECT id FROM corr_projects WHERE nombre = ?", (nombre.strip(),))
        project_id = int(cur.fetchone()[0])
        cur.execute("DELETE FROM corr_project_tags WHERE project_id = ?", (project_id,))
        for idx, t in enumerate(tags):
            cur.execute(
                """
                INSERT INTO corr_project_tags (project_id, tagid, codigo_tag, medidor_tipo, pos)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    int(t["tagid"]),
                    str(t["codigo_tag"]),
                    str(t["medidor_tipo"]),
                    idx,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def build_corr_sqlite(nombre: str) -> Tuple[str, int]:
    projects = {p["nombre"]: p for p in list_corr_projects()}
    project = projects.get(nombre)
    if not project:
        raise ValueError("Proyecto no encontrado")
    tags = project.get("tags", [])
    if not tags:
        raise ValueError("Proyecto sin tags")
    t_ini = datetime.fromisoformat(str(project["fecha_ini"]))
    t_fin = datetime.fromisoformat(str(project["fecha_fin"]))
    bins = build_time_bins(t_ini, t_fin, 15)
    if not bins:
        raise ValueError("Rango de fechas invalido")

    sqlite_path = Path(str(project["sqlite_file"]))
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sqlite_path)
    try:
        cur = conn.cursor()
        quoted_cols = ", ".join([f'"{t["codigo_tag"]}" REAL' for t in tags])
        cur.execute("DROP TABLE IF EXISTS correlation_data")
        cur.execute(
            f"""
            CREATE TABLE correlation_data (
                fecha TEXT PRIMARY KEY,
                {quoted_cols}
            )
            """
        )

        series_by_col: Dict[str, List[Optional[float]]] = {}
        for t in tags:
            raw = fetch_raw_values_mysql(int(t["tagid"]), t_ini, t_fin)
            med = compute_bin_averages(raw, bins, 15)
            interp = interpolate_missing(med)
            series_by_col[str(t["codigo_tag"])] = [interp.get(b) for b in bins]

        placeholders = ", ".join(["?"] * (1 + len(tags)))
        col_names = ", ".join([f'"{t["codigo_tag"]}"' for t in tags])
        insert_sql = f'INSERT INTO correlation_data (fecha, {col_names}) VALUES ({placeholders})'
        for i, b in enumerate(bins):
            row = [b.isoformat(timespec="minutes")]
            for t in tags:
                row.append(series_by_col[str(t["codigo_tag"])][i])
            cur.execute(insert_sql, tuple(row))
        conn.commit()
        return str(sqlite_path), len(bins)
    finally:
        conn.close()


def save_recinto_config(cfg: RecintoConfig) -> None:
    init_config_db()
    conn = sqlite3.connect(RT3_IA_CONFIG_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO recintos (nombre, tag_qin, tag_qin1, tag_qin2, tag_vol, tag_qout, point_qin1, point_qin2, point_vol, point_qout, source_type, volumen_maximo, activo, qout_unit, last_run_at, last_rows_saved)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(nombre) DO UPDATE SET
                tag_qin=excluded.tag_qin,
                tag_qin1=excluded.tag_qin1,
                tag_qin2=excluded.tag_qin2,
                tag_vol=excluded.tag_vol,
                tag_qout=excluded.tag_qout,
                point_qin1=excluded.point_qin1,
                point_qin2=excluded.point_qin2,
                point_vol=excluded.point_vol,
                point_qout=excluded.point_qout,
                source_type=excluded.source_type,
                volumen_maximo=excluded.volumen_maximo,
                activo=excluded.activo,
                qout_unit=excluded.qout_unit,
                last_run_at=excluded.last_run_at,
                last_rows_saved=excluded.last_rows_saved
            """,
            (
                cfg.nombre,
                cfg.tag_qin1 or 0,  # compatibilidad con esquema anterior
                cfg.tag_qin1 or 0,
                cfg.tag_qin2,
                cfg.tag_vol or 0,
                cfg.tag_qout or 0,
                cfg.point_qin1,
                cfg.point_qin2,
                cfg.point_vol,
                cfg.point_qout,
                cfg.source_type,
                cfg.volumen_maximo,
                1 if cfg.activo else 0,
                cfg.qout_unit,
                cfg.last_run_at,
                cfg.last_rows_saved,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_recinto_config(nombre: str) -> Optional[RecintoConfig]:
    init_config_db()
    if not RT3_IA_CONFIG_DB.exists():
        return None
    conn = sqlite3.connect(RT3_IA_CONFIG_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT nombre, tag_qin1, tag_qin2, tag_vol, tag_qout, point_qin1, point_qin2, point_vol, point_qout, source_type, volumen_maximo, activo, qout_unit, last_run_at, last_rows_saved FROM recintos WHERE nombre = ?",
            (nombre,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return RecintoConfig(
            nombre=row[0],
            tag_qin1=int(row[1]) if row[1] not in (None, 0) else None,
            tag_qin2=int(row[2]) if row[2] not in (None, 0) else None,
            tag_vol=int(row[3]) if row[3] not in (None, 0) else None,
            tag_qout=int(row[4]) if row[4] not in (None, 0) else None,
            point_qin1=row[5],
            point_qin2=row[6],
            point_vol=row[7],
            point_qout=row[8],
            source_type=row[9] or "influxdb",
            volumen_maximo=float(row[10]) if row[10] is not None else None,
            activo=bool(row[11]) if len(row) > 11 else True,
            qout_unit=row[12] if len(row) > 12 and row[12] is not None else "l/s",
            last_run_at=row[13] if len(row) > 13 else None,
            last_rows_saved=int(row[14]) if len(row) > 14 and row[14] is not None else None,
        )
    finally:
        conn.close()


def load_all_recintos() -> List[RecintoConfig]:
    # Asegurarse de que la tabla tenga todas las columnas (incluye migraciones)
    init_config_db()
    if not RT3_IA_CONFIG_DB.exists():
        return []
    conn = sqlite3.connect(RT3_IA_CONFIG_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT nombre, tag_qin1, tag_qin2, tag_vol, tag_qout, point_qin1, point_qin2, point_vol, point_qout, source_type, volumen_maximo, activo, qout_unit, last_run_at, last_rows_saved "
            "FROM recintos ORDER BY nombre"
        )
        out: List[RecintoConfig] = []
        for row in cur.fetchall():
            out.append(
                RecintoConfig(
                    nombre=row[0],
                    tag_qin1=int(row[1]) if row[1] not in (None, 0) else None,
                    tag_qin2=int(row[2]) if row[2] not in (None, 0) else None,
                    tag_vol=int(row[3]) if row[3] not in (None, 0) else None,
                    tag_qout=int(row[4]) if row[4] not in (None, 0) else None,
                    point_qin1=row[5],
                    point_qin2=row[6],
                    point_vol=row[7],
                    point_qout=row[8],
                    source_type=row[9] or "influxdb",
                    volumen_maximo=float(row[10]) if row[10] is not None else None,
                    activo=bool(row[11]) if len(row) > 11 else True,
                    qout_unit=row[12] if len(row) > 12 and row[12] is not None else "l/s",
                    last_run_at=row[13] if len(row) > 13 else None,
                    last_rows_saved=int(row[14]) if len(row) > 14 and row[14] is not None else None,
                )
            )
        return out
    finally:
        conn.close()


def set_recinto_activo(nombre: str, activo: bool) -> None:
    init_config_db()
    conn = sqlite3.connect(RT3_IA_CONFIG_DB)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE recintos SET activo = ? WHERE nombre = ?", (1 if activo else 0, nombre))
        conn.commit()
    finally:
        conn.close()


def delete_recinto(nombre: str) -> None:
    if not RT3_IA_CONFIG_DB.exists():
        return
    conn = sqlite3.connect(RT3_IA_CONFIG_DB)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM recintos WHERE nombre = ?", (nombre,))
        conn.commit()
    finally:
        conn.close()


def get_sqlite_row_count(nombre: str) -> int:
    """
    Devuelve cuántos registros hay en la tabla medidas del SQLite del recinto.
    """
    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM medidas")
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        conn.close()


def get_sqlite_min_fecha(nombre: str) -> Optional[datetime]:
    """
    Devuelve la primera fecha registrada en medidas del SQLite del recinto.
    """
    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT MIN(fecha) FROM medidas")
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        return datetime.fromisoformat(row[0])
    finally:
        conn.close()


def get_sqlite_last_fecha(nombre: str) -> Optional[datetime]:
    """
    Devuelve la última fecha registrada en medidas del SQLite del recinto.
    """
    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(fecha) FROM medidas")
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        # fecha se guarda como ISO 'YYYY-MM-DDTHH:MM'
        return datetime.fromisoformat(row[0])
    finally:
        conn.close()


@lru_cache(maxsize=128)
def get_sqlite_qout_scale(nombre: str) -> float:
    cfg = load_recinto_config(nombre.strip())
    if not cfg or cfg.qout_unit != "m3/hr":
        return 1.0

    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return 1.0

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT fecha, qin, vol, qout
            FROM medidas
            WHERE qin IS NOT NULL AND vol IS NOT NULL AND qout IS NOT NULL
            ORDER BY fecha DESC
            LIMIT 192
            """
        )
        sampled_rows = list(reversed(cur.fetchall()))
    except sqlite3.OperationalError:
        return 1.0
    finally:
        conn.close()

    if len(sampled_rows) < 4:
        return 1.0

    err_as_lps: List[float] = []
    err_as_m3hr: List[float] = []
    for idx in range(1, len(sampled_rows)):
        prev_fecha, _, prev_vol, _ = sampled_rows[idx - 1]
        fecha, qin, vol, qout = sampled_rows[idx]
        try:
            dt_prev = datetime.fromisoformat(str(prev_fecha))
            dt_cur = datetime.fromisoformat(str(fecha))
            elapsed_sec = (dt_cur - dt_prev).total_seconds()
            if elapsed_sec <= 0 or elapsed_sec > 1800:
                continue
            implied_qout_lps = float(qin) - ((float(vol) - float(prev_vol)) * 1000.0 / elapsed_sec)
            stored_qout = float(qout)
        except Exception:
            continue
        err_as_lps.append(abs(stored_qout - implied_qout_lps))
        err_as_m3hr.append(abs((stored_qout / 3.6) - implied_qout_lps))

    if len(err_as_lps) < 4 or len(err_as_m3hr) < 4:
        return 1.0

    median_err_lps = statistics.median(err_as_lps)
    median_err_m3hr = statistics.median(err_as_m3hr)
    if median_err_m3hr + 1e-6 < (median_err_lps * 0.6):
        return 1.0 / 3.6
    return 1.0


def normalize_sqlite_qout(nombre: str, value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return float(value) * get_sqlite_qout_scale(nombre)


def get_sqlite_last_medida(nombre: str) -> Optional[Tuple[datetime, Optional[float], Optional[float], Optional[float]]]:
    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT fecha, qin, vol, qout FROM medidas ORDER BY fecha DESC LIMIT 1")
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        return (
            datetime.fromisoformat(str(row[0])),
            row[1],
            row[2],
            normalize_sqlite_qout(nombre, row[3]),
        )
    finally:
        conn.close()


def get_sqlite_last_qin(nombre: str) -> Optional[Tuple[datetime, float]]:
    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT fecha, qin
            FROM medidas
            WHERE qin IS NOT NULL
            ORDER BY fecha DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if not row or row[0] is None or row[1] is None:
            return None
        return (datetime.fromisoformat(str(row[0])), float(row[1]))
    finally:
        conn.close()


def get_sqlite_max_volumen(nombre: str) -> Optional[float]:
    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(vol) FROM medidas")
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        return float(row[0])
    finally:
        conn.close()


def get_sqlite_max_qin(nombre: str) -> Optional[float]:
    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(qin) FROM medidas")
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        return float(row[0])
    finally:
        conn.close()


def get_sqlite_recent_medidas(
    nombre: str, limit: int = 96
) -> List[Tuple[datetime, Optional[float], Optional[float], Optional[float]]]:
    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT fecha, qin, vol, qout FROM medidas ORDER BY fecha DESC LIMIT ?",
            (int(limit),),
        )
        rows = cur.fetchall()
        out = []
        for fecha, qin, vol, qout in reversed(rows):
            out.append((datetime.fromisoformat(str(fecha)), qin, vol, normalize_sqlite_qout(nombre, qout)))
        return out
    finally:
        conn.close()


def get_sqlite_day_medidas(
    nombre: str, target_day: datetime
) -> List[Tuple[datetime, Optional[float], Optional[float], Optional[float]]]:
    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return []
    day_start = target_day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT fecha, qin, vol, qout
            FROM medidas
            WHERE fecha >= ? AND fecha < ?
            ORDER BY fecha ASC
            """,
            (
                day_start.isoformat(timespec="minutes"),
                day_end.isoformat(timespec="minutes"),
            ),
        )
        out = []
        for fecha, qin, vol, qout in cur.fetchall():
            out.append((datetime.fromisoformat(str(fecha)), qin, vol, normalize_sqlite_qout(nombre, qout)))
        return out
    finally:
        conn.close()


def get_sqlite_range_medidas(
    nombre: str, start_dt: datetime, end_dt: datetime
) -> List[Tuple[datetime, Optional[float], Optional[float], Optional[float]]]:
    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT fecha, qin, vol, qout
            FROM medidas
            WHERE fecha >= ? AND fecha <= ?
            ORDER BY fecha ASC
            """,
            (
                start_dt.isoformat(timespec="minutes"),
                end_dt.isoformat(timespec="minutes"),
            ),
        )
        out = []
        for fecha, qin, vol, qout in cur.fetchall():
            out.append((datetime.fromisoformat(str(fecha)), qin, vol, normalize_sqlite_qout(nombre, qout)))
        return out
    finally:
        conn.close()


def load_perfil_consumo(nombre: str, profile_type: str = "weekday") -> List[Dict[str, Optional[float]]]:
    selected_profile_type = (profile_type or "weekday").strip().lower()
    if selected_profile_type == "weekend":
        saturday_rows = load_perfil_consumo(nombre, "saturday")
        sunday_rows = load_perfil_consumo(nombre, "sunday")
        weekend_slots: Dict[int, List[Dict[str, Optional[float]]]] = {}
        for rows in (saturday_rows, sunday_rows):
            for row in rows:
                weekend_slots.setdefault(int(row["slot_index"]), []).append(row)
        if weekend_slots:
            merged_rows: List[Dict[str, Optional[float]]] = []
            for slot_index in sorted(weekend_slots.keys()):
                slot_rows = weekend_slots[slot_index]
                qout_promedio_vals = [float(r["qout_promedio"]) for r in slot_rows if r.get("qout_promedio") is not None]
                qout_ia_vals = [float(r["qout_ia"]) for r in slot_rows if r.get("qout_ia") is not None]
                merged_rows.append(
                    {
                        "profile_type": "weekend",
                        "profile_label": "Fin de semana",
                        "slot_index": slot_index,
                        "hora_texto": str(slot_rows[0]["hora_texto"]),
                        "qout_promedio": (sum(qout_promedio_vals) / len(qout_promedio_vals)) if qout_promedio_vals else None,
                        "qout_ia": (sum(qout_ia_vals) / len(qout_ia_vals)) if qout_ia_vals else None,
                    }
                )
            return merged_rows
        if saturday_rows:
            return saturday_rows
        if sunday_rows:
            return sunday_rows
        return load_perfil_consumo(nombre, "weekday")

    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(perfil_consumo_15min)")
        cols = [row[1] for row in cur.fetchall()]
        has_profile_type = "profile_type" in cols
        if selected_profile_type not in {"weekday", "saturday", "sunday"}:
            selected_profile_type = "weekday"
        qout_scale = get_sqlite_qout_scale(nombre)
        if has_profile_type:
            cur.execute(
                """
                SELECT profile_type, profile_label, slot_index, hora_texto, qout_promedio, qout_ia
                FROM perfil_consumo_15min
                WHERE profile_type = ?
                ORDER BY slot_index
                """,
                (selected_profile_type,),
            )
        else:
            cur.execute(
                """
                SELECT 'weekday' AS profile_type, 'Lunes a viernes' AS profile_label, slot_index, hora_texto, qout_promedio, qout_ia
                FROM perfil_consumo_15min
                ORDER BY slot_index
                """
            )
        rows = []
        for profile_type, profile_label, slot_index, hora_texto, qout_promedio, qout_ia in cur.fetchall():
            rows.append(
                {
                    "profile_type": str(profile_type),
                    "profile_label": str(profile_label),
                    "slot_index": int(slot_index),
                    "hora_texto": str(hora_texto),
                    "qout_promedio": (float(qout_promedio) * qout_scale) if qout_promedio is not None else None,
                    "qout_ia": (float(qout_ia) * qout_scale) if qout_ia is not None else None,
                }
            )
        if not rows and has_profile_type and selected_profile_type != "weekday":
            cur.execute(
                """
                SELECT profile_type, profile_label, slot_index, hora_texto, qout_promedio, qout_ia
                FROM perfil_consumo_15min
                WHERE profile_type = 'weekday'
                ORDER BY slot_index
                """
            )
            for profile_type, profile_label, slot_index, hora_texto, qout_promedio, qout_ia in cur.fetchall():
                rows.append(
                    {
                        "profile_type": str(profile_type),
                        "profile_label": str(profile_label),
                        "slot_index": int(slot_index),
                        "hora_texto": str(hora_texto),
                        "qout_promedio": (float(qout_promedio) * qout_scale) if qout_promedio is not None else None,
                        "qout_ia": (float(qout_ia) * qout_scale) if qout_ia is not None else None,
                    }
                )
        return rows
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

def profile_type_for_chile_day(dt: datetime) -> str:
    if dt.weekday() == 5:
        return "saturday"
    if dt.weekday() == 6:
        return "sunday"
    return "weekday"


def profile_group_for_chile_day(dt: datetime) -> str:
    return "weekend" if dt.weekday() >= 5 else "weekday"


def profile_group_label(profile_group: str) -> str:
    return "Fin de semana" if (profile_group or "").strip().lower() == "weekend" else "Lunes a viernes"


def load_perfil_metadata(nombre: str) -> Dict[str, str]:
    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM perfil_consumo_15min_meta")
        return {str(k): "" if v is None else str(v) for k, v in cur.fetchall()}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def is_recent_training(nombre: str, max_age_hours: int = 24) -> bool:
    meta = load_perfil_metadata(nombre)
    trained_at_raw = meta.get("trained_at")
    if not trained_at_raw:
        return False
    try:
        trained_at = datetime.fromisoformat(str(trained_at_raw).strip('"'))
    except Exception:
        return False
    return (datetime.now() - trained_at) <= timedelta(hours=max_age_hours)


def fecha_fin_sqlite_estado(fecha_fin: Optional[datetime]) -> str:
    """
    Devuelve el estado visual para la fecha fin SQLite comparada contra hora Chile.
    - "fresh": diferencia <= 30 min
    - "stale": diferencia > 30 min
    - "unknown": sin fecha
    """
    if fecha_fin is None:
        return "unknown"
    now_chile = datetime.now(CHILE_TZ).replace(tzinfo=None)
    diff_minutes = abs((now_chile - fecha_fin).total_seconds()) / 60.0
    return "fresh" if diff_minutes <= 30 else "stale"


def get_sqlite_context_start_for_append(nombre: str, max_lookback_rows: int = 500) -> Optional[datetime]:
    """
    Para append: si al final del SQLite hay "bloques vacíos" (qin/vol/qout NULL),
    devolvemos la fecha del último registro NO vacío (desde el final).

    Esto da contexto a la interpolación para rellenar bins vacíos aunque el append
    se dispare justo al borde de 15 minutos (sin bins posteriores en el rango).
    """
    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return None

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT fecha, qin, vol, qout FROM medidas ORDER BY fecha DESC LIMIT ?",
            (int(max_lookback_rows),),
        )
        rows = cur.fetchall()
        if not rows:
            return None

        # Encontrar el primer registro desde el final que NO sea "vacío" (no todos NULL)
        for fecha, qin, vol, qout in rows:
            if not (qin is None and vol is None and qout is None):
                return datetime.fromisoformat(str(fecha))

        # Si todo está vacío, devolvemos la última fecha disponible
        last_fecha = rows[0][0]
        return datetime.fromisoformat(str(last_fecha))
    finally:
        conn.close()


def get_sqlite_missing_context_start_for_repair(
    nombre: str, max_lookback_rows: int = 2000
) -> Optional[datetime]:
    """
    Busca filas recientes incompletas (alguno de qin/vol/qout en NULL) y devuelve
    una fecha de inicio apropiada para reprocesarlas con contexto.

    Si encuentra una fila completa justo antes del primer hueco, devuelve esa fecha
    para favorecer la interpolacion. Si no, devuelve la fecha del primer hueco.
    """
    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return None

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT fecha, qin, vol, qout FROM medidas ORDER BY fecha DESC LIMIT ?",
            (int(max_lookback_rows),),
        )
        rows = cur.fetchall()
        if not rows:
            return None

        rows = list(reversed(rows))
        first_incomplete_idx: Optional[int] = None
        for idx, (fecha, qin, vol, qout) in enumerate(rows):
            if qin is None or vol is None or qout is None:
                first_incomplete_idx = idx
                break

        if first_incomplete_idx is None:
            return None

        # Buscar una fila completa previa para dar contexto a la interpolacion.
        for idx in range(first_incomplete_idx - 1, -1, -1):
            fecha, qin, vol, qout = rows[idx]
            if qin is not None and vol is not None and qout is not None:
                return datetime.fromisoformat(str(fecha))

        fecha = rows[first_incomplete_idx][0]
        return datetime.fromisoformat(str(fecha))
    finally:
        conn.close()


def clear_recinto_medidas(nombre: str) -> None:
    """
    Elimina todos los registros de medidas del SQLite del recinto.
    """
    db_path = ensure_recinto_db(nombre)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM medidas")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers SQLite por recinto
# ---------------------------------------------------------------------------


def get_recinto_db_path(nombre: str) -> Path:
    ensure_dirs()
    safe_name = nombre.strip().lower()
    return RT3_IA_DATA_DIR / f"{safe_name}.sqlite"


def ensure_recinto_db(nombre: str) -> Path:
    db_path = get_recinto_db_path(nombre)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS medidas (
                fecha TEXT PRIMARY KEY,
                qin REAL,
                vol REAL,
                qout REAL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def insert_medidas_batch(
    nombre: str, datos: List[Tuple[datetime, Optional[float], Optional[float], Optional[float]]]
) -> int:
    """
    Inserta/actualiza lote de medidas en SQLite del recinto.
    """
    db_path = ensure_recinto_db(nombre)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        rows = [
            (dt.isoformat(timespec="minutes"), qin, vol, qout)
            for dt, qin, vol, qout in datos
        ]
        cur.executemany(
            """
            INSERT INTO medidas (fecha, qin, vol, qout)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(fecha) DO UPDATE SET
                qin=excluded.qin,
                vol=excluded.vol,
                qout=excluded.qout
            """,
            rows,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_sqlite_medidas_map(
    nombre: str, t_ini: datetime, t_fin: datetime
) -> Dict[str, Tuple[Optional[float], Optional[float], Optional[float]]]:
    """
    Devuelve un mapa fecha_iso_minuto -> (qin, vol, qout) para el rango indicado.
    """
    db_path = get_recinto_db_path(nombre)
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT fecha, qin, vol, qout
            FROM medidas
            WHERE fecha >= ? AND fecha <= ?
            """,
            (t_ini.isoformat(timespec="minutes"), t_fin.isoformat(timespec="minutes")),
        )
        return {
            str(fecha): (qin, vol, qout)
            for fecha, qin, vol, qout in cur.fetchall()
        }
    finally:
        conn.close()


def merge_append_series_preserving_existing(
    nombre: str,
    datos: List[Tuple[datetime, Optional[float], Optional[float], Optional[float]]],
) -> List[Tuple[datetime, Optional[float], Optional[float], Optional[float]]]:
    """
    En modo append no debemos pisar filas ya completas del SQLite.
    Solo se actualizan filas nuevas o filas vacías/incompletas.
    """
    if not datos:
        return datos
    existing = get_sqlite_medidas_map(nombre, datos[0][0], datos[-1][0])
    merged: List[Tuple[datetime, Optional[float], Optional[float], Optional[float]]] = []
    for dt, qin, vol, qout in datos:
        key = dt.isoformat(timespec="minutes")
        prev = existing.get(key)
        if prev is None:
            merged.append((dt, qin, vol, qout))
            continue
        prev_qin, prev_vol, prev_qout = prev
        prev_has_data = any(v is not None for v in prev)
        prev_complete = all(v is not None for v in prev)
        new_values = (qin, vol, qout)
        if prev_complete:
            merged.append((dt, prev_qin, prev_vol, prev_qout))
            continue
        merged.append(
            (
                dt,
                prev_qin if prev_qin is not None else qin,
                prev_vol if prev_vol is not None else vol,
                prev_qout if prev_qout is not None else qout,
            )
            if prev_has_data
            else (dt, qin, vol, qout)
        )
    return merged


def align_next_15(dt: datetime) -> datetime:
    base = dt.replace(second=0, microsecond=0)
    remainder = base.minute % 15
    if remainder == 0:
        return base + timedelta(minutes=15)
    return base + timedelta(minutes=(15 - remainder))


def slot_index_from_datetime(dt: datetime) -> int:
    return dt.hour * 4 + (dt.minute // 15)


def build_projection_24h(nombre: str, profile_type: str = "weekday") -> List[Dict[str, Optional[float]]]:
    perfil = load_perfil_consumo(nombre, profile_type=profile_type)
    ultima = get_sqlite_last_medida(nombre)
    if not perfil or ultima is None:
        return []

    perfil_by_slot = {int(row["slot_index"]): row for row in perfil}
    start_dt = align_next_15(ultima[0])
    out: List[Dict[str, Optional[float]]] = []

    for i in range(96):
        ts = start_dt + timedelta(minutes=15 * i)
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


def simulate_volume_projection(
    initial_volume: Optional[float],
    qin_fixed: float,
    projection_rows: List[Dict[str, Optional[float]]],
    qout_key: str,
) -> List[Dict[str, Optional[float]]]:
    if initial_volume is None:
        return []
    vol = max(0.0, float(initial_volume))
    out: List[Dict[str, Optional[float]]] = []
    for row in projection_rows:
        qout = row.get(qout_key)
        qout_val = float(qout) if qout is not None else 0.0
        vol = max(0.0, vol + ((float(qin_fixed) - qout_val) * 900.0 / 1000.0))
        out.append(
            {
                "timestamp": row["timestamp"],
                "hora_texto": row["hora_texto"],
                "volumen": vol,
                "qout": qout,
            }
        )
    return out


def compute_required_fixed_qin(
    current_volume: Optional[float],
    target_volume: Optional[float],
    projection_rows: List[Dict[str, Optional[float]]],
    qout_key: str = "qout_ia",
) -> Optional[float]:
    if current_volume is None or target_volume is None or not projection_rows:
        return None
    steps = len(projection_rows)
    if steps <= 0:
        return None
    total_qout_m3 = 0.0
    for row in projection_rows:
        qout = row.get(qout_key)
        total_qout_m3 += (float(qout) if qout is not None else 0.0) * 900.0 / 1000.0
    required_total_in_m3 = (float(target_volume) - float(current_volume)) + total_qout_m3
    qin_required = required_total_in_m3 / (steps * 900.0 / 1000.0)
    return max(0.0, qin_required)


def compute_constrained_qin_ideal(
    initial_volume: Optional[float],
    target_upper: Optional[float],
    projection_rows: List[Dict[str, Optional[float]]],
    qout_key: str = "qout_ia",
    qin_cap: Optional[float] = None,
) -> Optional[float]:
    if initial_volume is None or target_upper is None or not projection_rows:
        return None

    cap = float(qin_cap) if qin_cap is not None and qin_cap > 0 else 200.0
    target_upper_f = float(target_upper)
    # Tolerancia para decidir si ya estamos "en el pico" actual.
    peak_tol = 1e-6
    at_current_peak = float(initial_volume) >= (target_upper_f - peak_tol)

    def projected_peak_metric(qin_value: float) -> float:
        vol = float(initial_volume)
        max_vol = vol
        dropped_below_target = vol < (target_upper_f - peak_tol)
        max_after_drop: Optional[float] = None
        for row in projection_rows:
            qout = row.get(qout_key)
            qout_val = float(qout) if qout is not None else 0.0
            vol = vol + ((float(qin_value) - qout_val) * 900.0 / 1000.0)
            if vol > max_vol:
                max_vol = vol
            # Si ya estamos en el pico actual, evaluar el siguiente pico:
            # primero debe caer bajo el umbral y luego medir maximo posterior.
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

    lo = 0.0
    hi = cap
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if projected_peak_metric(mid) <= target_upper_f:
            lo = mid
        else:
            hi = mid
    return lo


def safe_float(value: Optional[str], default: Optional[float] = None) -> Optional[float]:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def simulate_day_volume_projection(
    initial_volume: Optional[float],
    qin_fixed: float,
    perfil: List[Dict[str, Optional[float]]],
    qout_key: str,
) -> List[Optional[float]]:
    if initial_volume is None:
        return [None for _ in perfil]
    vol = max(0.0, float(initial_volume))
    out: List[Optional[float]] = []
    for row in perfil:
        qout = row.get(qout_key)
        qout_val = float(qout) if qout is not None else 0.0
        vol = max(0.0, vol + ((float(qin_fixed) - qout_val) * 900.0 / 1000.0))
        out.append(vol)
    return out


def simulate_day_volume_projection_from_slot(
    initial_volume: Optional[float],
    qin_fixed: float,
    perfil: List[Dict[str, Optional[float]]],
    qout_key: str,
    start_slot: int,
) -> List[Optional[float]]:
    out: List[Optional[float]] = [None for _ in perfil]
    if initial_volume is None or not perfil:
        return out
    if start_slot < 0:
        start_slot = 0
    if start_slot >= len(perfil):
        return out

    vol = max(0.0, float(initial_volume))
    out[start_slot] = vol
    for slot in range(start_slot + 1, len(perfil)):
        qout = perfil[slot].get(qout_key)
        qout_val = float(qout) if qout is not None else 0.0
        vol = max(0.0, vol + ((float(qin_fixed) - qout_val) * 900.0 / 1000.0))
        out[slot] = vol
    return out


def advance_day_volume_to_slot(
    initial_volume: Optional[float],
    qin_fixed: float,
    perfil: List[Dict[str, Optional[float]]],
    qout_key: str,
    start_slot: int,
    target_slot: int,
) -> Optional[float]:
    if initial_volume is None or not perfil:
        return initial_volume

    safe_start = max(0, min(len(perfil) - 1, int(start_slot)))
    safe_target = max(0, min(len(perfil) - 1, int(target_slot)))
    vol = max(0.0, float(initial_volume))
    if safe_target <= safe_start:
        return vol

    for slot in range(safe_start + 1, safe_target + 1):
        qout = perfil[slot].get(qout_key)
        qout_val = float(qout) if qout is not None else 0.0
        vol = max(0.0, vol + ((float(qin_fixed) - qout_val) * 900.0 / 1000.0))
    return vol


def find_volume_threshold_event(
    initial_volume: Optional[float],
    qin_fixed: Optional[float],
    perfil: List[Dict[str, Optional[float]]],
    qout_key: str,
    start_dt: datetime,
    threshold_volume: Optional[float],
    direction: str,
    horizon_days: int = 7,
) -> Optional[Dict[str, object]]:
    if initial_volume is None or qin_fixed is None or threshold_volume is None or not perfil:
        return None

    threshold = float(threshold_volume)
    vol = max(0.0, float(initial_volume))
    safe_direction = "above" if direction == "above" else "below"
    if (safe_direction == "above" and vol >= threshold) or (safe_direction == "below" and vol <= threshold):
        return {"status": "now", "event_dt": start_dt, "event_volume": vol, "threshold_volume": threshold}

    perfil_by_slot = {int(row["slot_index"]): row for row in perfil}
    current_dt = start_dt
    total_steps = max(1, int(horizon_days) * 96)
    for _ in range(total_steps):
        current_dt = current_dt + timedelta(minutes=15)
        slot_index = slot_index_from_datetime(current_dt)
        qout = perfil_by_slot.get(slot_index, {}).get(qout_key)
        qout_val = float(qout) if qout is not None else 0.0
        vol = max(0.0, vol + ((float(qin_fixed) - qout_val) * 900.0 / 1000.0))
        if safe_direction == "above" and vol >= threshold:
            return {"status": "event", "event_dt": current_dt, "event_volume": vol, "threshold_volume": threshold}
        if safe_direction == "below" and vol <= threshold:
            return {"status": "event", "event_dt": current_dt, "event_volume": vol, "threshold_volume": threshold}

    return {
        "status": "not_found",
        "event_dt": None,
        "event_volume": vol,
        "threshold_volume": threshold,
        "horizon_days": int(horizon_days),
    }


def format_duration_compact(delta: timedelta) -> str:
    total_minutes = max(0, int(round(delta.total_seconds() / 60.0)))
    days, rem = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days} d")
    if hours:
        parts.append(f"{hours} h")
    if minutes or not parts:
        parts.append(f"{minutes} min")
    return " ".join(parts)


def format_event_datetime(dt: Optional[datetime]) -> str:
    if dt is None:
        return "-"
    return dt.strftime("%d/%m %H:%M")


def build_volume_event_card(
    title: str,
    event: Optional[Dict[str, object]],
    reference_dt: datetime,
    qin_value: Optional[float],
    qin_label: str = "qin",
) -> Dict[str, str]:
    if event is None:
        return {"title": title, "value": "-", "sub": "No hay datos suficientes para estimar este umbral."}

    qin_text = f'{float(qin_value):.2f} l/s' if qin_value is not None else "-"
    threshold_volume = event.get("threshold_volume")
    threshold_text = f'{float(threshold_volume):.2f} m3' if threshold_volume is not None else "-"
    status = str(event.get("status") or "")
    if status == "now":
        event_dt = event.get("event_dt")
        if isinstance(event_dt, datetime) and event_dt > reference_dt:
            duration_text = format_duration_compact(event_dt - reference_dt)
            return {
                "title": title,
                "value": format_event_datetime(event_dt),
                "sub": f'En {duration_text} con {qin_label} {qin_text}. Umbral: {threshold_text}.',
            }
        return {
            "title": title,
            "value": "Ahora",
            "sub": f'Umbral en {threshold_text} ya alcanzado a las {format_event_datetime(event_dt)} con {qin_label} {qin_text}.',
        }
    if status == "event":
        event_dt = event.get("event_dt")
        duration_text = format_duration_compact(event_dt - reference_dt) if isinstance(event_dt, datetime) else "-"
        return {
            "title": title,
            "value": format_event_datetime(event_dt if isinstance(event_dt, datetime) else None),
            "sub": f'En {duration_text} con {qin_label} {qin_text}. Umbral: {threshold_text}.',
        }
    horizon_days = int(event.get("horizon_days") or 0)
    return {
        "title": title,
        "value": f"No en {horizon_days} días" if horizon_days > 0 else "No proyectado",
        "sub": f'Con {qin_label} {qin_text} no cruza el umbral {threshold_text} dentro del horizonte simulado.',
    }


def serialize_volume_event(event: Optional[Dict[str, object]]) -> Optional[Dict[str, object]]:
    if event is None:
        return None
    out = dict(event)
    event_dt = out.get("event_dt")
    if isinstance(event_dt, datetime):
        out["event_dt"] = event_dt.isoformat(timespec="minutes")
    return out


def normalize_export_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): normalize_export_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_export_value(item) for item in value]
    if isinstance(value, tuple):
        return [normalize_export_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat(timespec="minutes")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            looks_like_json = stripped[0] in {'"', "{", "["} or stripped in {"true", "false", "null"}
            looks_like_number = bool(re.fullmatch(r"-?\d+(?:\.\d+)?", stripped))
            if looks_like_json or looks_like_number:
                try:
                    return normalize_export_value(json.loads(stripped))
                except Exception:
                    return value
        return value
    return value


def build_recinto_ia_snapshot(nombre: str, profile_group: Optional[str] = None) -> Optional[Dict[str, object]]:
    cfg = load_recinto_config(nombre.strip())
    if not cfg:
        return None

    now_chile = datetime.now(CHILE_TZ)
    chile_today = now_chile.replace(tzinfo=None)
    default_profile_group = profile_group_for_chile_day(chile_today)
    selected_profile_group = (profile_group or default_profile_group).strip().lower()
    if selected_profile_group not in {"weekday", "weekend"}:
        selected_profile_group = default_profile_group

    perfil = load_perfil_consumo(cfg.nombre, profile_type=selected_profile_group)
    perfil_weekday = load_perfil_consumo(cfg.nombre, profile_type="weekday")
    perfil_weekend = load_perfil_consumo(cfg.nombre, profile_type="weekend")
    meta = normalize_export_value(load_perfil_metadata(cfg.nombre))
    ultima = get_sqlite_last_medida(cfg.nombre)
    ultima_qin = get_sqlite_last_qin(cfg.nombre)
    max_vol_ideal = get_sqlite_max_volumen(cfg.nombre)
    max_qin_historico = get_sqlite_max_qin(cfg.nombre)

    last_fecha = ultima[0].isoformat(timespec="minutes") if ultima is not None else None
    last_qin = ultima_qin[1] if ultima_qin is not None else (ultima[1] if ultima is not None else None)
    last_qin_fecha = ultima_qin[0].isoformat(timespec="minutes") if ultima_qin is not None else (last_fecha if last_qin is not None else None)
    last_vol = ultima[2] if ultima is not None else None

    volumen_banda_min = None
    volumen_banda_max = None
    if cfg.volumen_maximo is not None and cfg.volumen_maximo > 0:
        volumen_banda_min = cfg.volumen_maximo * 0.50
        volumen_banda_max = cfg.volumen_maximo * 0.90
    target_vol = volumen_banda_max if volumen_banda_max is not None else max_vol_ideal

    qin_actual = float(last_qin) if last_qin is not None else 0.0
    projection_rows = build_projection_24h(cfg.nombre, profile_type=selected_profile_group)
    projection_avg = simulate_volume_projection(last_vol, qin_actual, projection_rows, "qout_promedio")
    projection_current = simulate_volume_projection(last_vol, qin_actual, projection_rows, "qout_ia")
    qin_ideal = compute_constrained_qin_ideal(
        last_vol,
        target_vol,
        projection_rows,
        "qout_ia",
        max_qin_historico,
    )
    qin_ideal_base = compute_required_fixed_qin(last_vol, target_vol, projection_rows, "qout_ia")

    today_rows = get_sqlite_day_medidas(cfg.nombre, chile_today)
    initial_day_volume = None
    latest_today_dt = None
    if today_rows:
        for dt, _, vol, _ in reversed(today_rows):
            if vol is not None:
                initial_day_volume = float(vol)
                latest_today_dt = dt
                break
    if initial_day_volume is None:
        initial_day_volume = last_vol
        latest_today_dt = ultima[0] if ultima is not None else None

    day_start = chile_today.replace(hour=0, minute=0, second=0, microsecond=0)
    day_profile_labels, day_profile_tooltip_dates, extended_profile = build_extended_profile_window(perfil, day_start, extra_hours=12)
    current_chile_slot_float = (
        now_chile.hour * 4
        + (now_chile.minute / 15.0)
        + (now_chile.second / 900.0)
    )
    latest_today_slot = slot_index_from_datetime(latest_today_dt) if latest_today_dt is not None else 0
    projection_start_slot = min(len(extended_profile) - 1, max(latest_today_slot, int(math.ceil(current_chile_slot_float)))) if extended_profile else 0
    projection_start_dt = day_start + timedelta(minutes=15 * projection_start_slot)

    projection_start_volume_current = advance_day_volume_to_slot(
        initial_day_volume,
        qin_actual,
        extended_profile,
        "qout_ia",
        latest_today_slot,
        projection_start_slot,
    ) if extended_profile else initial_day_volume
    projection_start_volume_qin_ideal = advance_day_volume_to_slot(
        initial_day_volume,
        qin_ideal,
        extended_profile,
        "qout_ia",
        latest_today_slot,
        projection_start_slot,
    ) if extended_profile and qin_ideal is not None else None

    day_projection_vol_ia = simulate_day_volume_projection_from_slot(
        projection_start_volume_current,
        qin_actual,
        extended_profile,
        "qout_ia",
        projection_start_slot,
    )
    day_projection_vol_ideal_ia = build_sinusoidal_ideal_volume_series(
        extended_profile,
        "qout_ia",
        volumen_banda_min,
        volumen_banda_max,
    )
    today_vol_by_slot = {
        slot_index_from_datetime(dt): (float(vol) if vol is not None else None)
        for dt, _, vol, _ in today_rows
    }
    real_today_vol = [today_vol_by_slot.get(slot) if slot < 96 else None for slot in range(len(extended_profile))]
    chart_vol_vertical_lines = [
        {"index": current_chile_slot_float, "color": "#c62828", "dash": ""},
        {"index": 0, "color": "#9ca3af", "dash": "5 5"},
        {"index": 96, "color": "#9ca3af", "dash": "5 5"},
    ]

    volumen_rebalse = cfg.volumen_maximo if cfg.volumen_maximo is not None and cfg.volumen_maximo > 0 else None
    volumen_bajo_10 = (cfg.volumen_maximo * 0.10) if cfg.volumen_maximo is not None and cfg.volumen_maximo > 0 else None
    rebalse_actual_event = find_volume_threshold_event(
        projection_start_volume_current,
        qin_actual,
        perfil,
        "qout_ia",
        projection_start_dt,
        volumen_rebalse,
        "above",
    )
    bajo_10_actual_event = find_volume_threshold_event(
        projection_start_volume_current,
        qin_actual,
        perfil,
        "qout_ia",
        projection_start_dt,
        volumen_bajo_10,
        "below",
    )
    rebalse_qin_ideal_event = find_volume_threshold_event(
        projection_start_volume_qin_ideal,
        qin_ideal,
        perfil,
        "qout_ia",
        projection_start_dt,
        volumen_rebalse,
        "above",
    )
    bajo_10_qin_ideal_event = find_volume_threshold_event(
        projection_start_volume_qin_ideal,
        qin_ideal,
        perfil,
        "qout_ia",
        projection_start_dt,
        volumen_bajo_10,
        "below",
    )

    projection_table: List[Dict[str, object]] = []
    for idx, row in enumerate(projection_rows):
        projection_table.append(
            {
                "timestamp": row["timestamp"],
                "hora_texto": row["hora_texto"],
                "qout_promedio": row["qout_promedio"],
                "qout_ia": row["qout_ia"],
                "vol_promedio": projection_avg[idx]["volumen"] if idx < len(projection_avg) else None,
                "vol_ia": projection_current[idx]["volumen"] if idx < len(projection_current) else None,
            }
        )

    projection_points: List[Dict[str, object]] = []
    for idx, tooltip_date in enumerate(day_profile_tooltip_dates):
        projection_points.append(
            {
                "timestamp": tooltip_date,
                "label": day_profile_labels[idx] if idx < len(day_profile_labels) else None,
                "vol_ia": day_projection_vol_ia[idx] if idx < len(day_projection_vol_ia) else None,
                "vol_ideal": day_projection_vol_ideal_ia[idx] if idx < len(day_projection_vol_ideal_ia) else None,
                "vol_real": real_today_vol[idx] if idx < len(real_today_vol) else None,
            }
        )

    projection_labels = [row["hora_texto"] for row in projection_rows]
    projection_qout_avg = [row["qout_promedio"] for row in projection_rows]
    projection_qout_ia = [row["qout_ia"] for row in projection_rows]
    projection_vol_avg = [row["volumen"] for row in projection_avg]
    projection_vol_ia = [row["volumen"] for row in projection_current]
    profile_chart_source = perfil_weekday if perfil_weekday else (perfil_weekend if perfil_weekend else perfil)
    profile_labels = [str(row["hora_texto"]) for row in profile_chart_source]
    profile_qout_weekday_ia = [row["qout_ia"] for row in perfil_weekday]
    profile_qout_weekend_ia = [row["qout_ia"] for row in perfil_weekend]
    profile_qout_ia = [row["qout_ia"] for row in perfil]
    qout_ia_samples = [(idx, float(value)) for idx, value in enumerate(profile_qout_ia) if value is not None]
    chart_qout_vertical_lines = []
    if qout_ia_samples:
        max_idx, _ = max(qout_ia_samples, key=lambda item: item[1])
        min_idx, _ = min(qout_ia_samples, key=lambda item: item[1])
        chart_qout_vertical_lines.append({"index": max_idx, "color": "#c62828", "dash": "6 4"})
        chart_qout_vertical_lines.append({"index": min_idx, "color": "#616161", "dash": "6 4"})
    yesterday_rows = get_sqlite_day_medidas(cfg.nombre, chile_today - timedelta(days=1))
    prev_day_qout_map = {
        slot_index_from_datetime(dt): (float(qout) if qout is not None else None)
        for dt, _, _, qout in yesterday_rows
    }
    prev_day_qout_real = [prev_day_qout_map.get(slot) for slot in range(96)]
    recent = get_sqlite_recent_medidas(cfg.nombre, limit=96)
    hist_labels = [dt.strftime("%H:%M") for dt, _, _, _ in recent]
    hist_qin = [qin for _, qin, _, _ in recent]
    hist_qout = [qout for _, _, _, qout in recent]
    reference_now_dt = now_chile.replace(tzinfo=None)

    return {
        "generated_at": now_chile.replace(tzinfo=None).isoformat(timespec="minutes"),
        "recinto": cfg.nombre,
        "profile_group": selected_profile_group,
        "profile_label": profile_group_label(selected_profile_group),
        "meta": meta,
        "cards": {
            "last_fecha": last_fecha,
            "last_qin": last_qin,
            "last_qin_fecha": last_qin_fecha,
            "last_vol": last_vol,
            "max_qin_historico": max_qin_historico,
            "max_vol_ideal": max_vol_ideal,
            "volumen_maximo": cfg.volumen_maximo,
            "volumen_banda_min": volumen_banda_min,
            "volumen_banda_max": volumen_banda_max,
            "target_vol": target_vol,
            "qin_ideal": qin_ideal,
            "qin_ideal_base": qin_ideal_base,
            "rebalse_with_qin_actual_card": build_volume_event_card(
                "Rebalse con qin actual",
                rebalse_actual_event,
                reference_now_dt,
                qin_actual,
                "qin actual",
            ),
            "bajo_10_with_qin_actual_card": build_volume_event_card(
                "Bajo 10% con qin actual",
                bajo_10_actual_event,
                reference_now_dt,
                qin_actual,
                "qin actual",
            ),
            "rebalse_with_qin_ideal_card": build_volume_event_card(
                "Rebalse con qin ideal",
                rebalse_qin_ideal_event,
                reference_now_dt,
                qin_ideal,
                "qin ideal",
            ),
            "bajo_10_with_qin_ideal_card": build_volume_event_card(
                "Bajo 10% con qin ideal",
                bajo_10_qin_ideal_event,
                reference_now_dt,
                qin_ideal,
                "qin ideal",
            ),
        },
        "summary": {
            "last_fecha": last_fecha,
            "last_qin": last_qin,
            "last_qin_fecha": last_qin_fecha,
            "last_vol": last_vol,
            "max_qin_historico": max_qin_historico,
            "max_vol_ideal": max_vol_ideal,
            "volumen_maximo": cfg.volumen_maximo,
            "volumen_banda_min": volumen_banda_min,
            "volumen_banda_max": volumen_banda_max,
            "target_vol": target_vol,
        },
        "projection": {
            "qin_actual": qin_actual,
            "qin_ideal": qin_ideal,
            "qin_ideal_base": qin_ideal_base,
            "start_dt": projection_start_dt.isoformat(timespec="minutes"),
            "projection_table_24h": projection_table,
            "projection_points": projection_points,
            "rebalse_with_qin_actual": serialize_volume_event(rebalse_actual_event),
            "bajo_10_with_qin_actual": serialize_volume_event(bajo_10_actual_event),
            "rebalse_with_qin_ideal": serialize_volume_event(rebalse_qin_ideal_event),
            "bajo_10_with_qin_ideal": serialize_volume_event(bajo_10_qin_ideal_event),
        },
        "charts": {
            "volume_today": {
                "labels": day_profile_labels,
                "tooltip_dates": day_profile_tooltip_dates,
                "vol_real": real_today_vol,
                "vol_ia": day_projection_vol_ia,
                "vol_ideal_ia": day_projection_vol_ideal_ia,
                "vertical_lines": chart_vol_vertical_lines,
                "current_slot_float": current_chile_slot_float,
                "latest_today_slot": latest_today_slot,
                "projection_start_slot": projection_start_slot,
                "projection_start_dt": projection_start_dt.isoformat(timespec="minutes"),
            },
            "projection_24h": {
                "labels": projection_labels,
                "qout_promedio": projection_qout_avg,
                "qout_ia": projection_qout_ia,
                "vol_promedio": projection_vol_avg,
                "vol_ia": projection_vol_ia,
            },
            "perfil_consumo": {
                "labels": profile_labels,
                "ia_profile_selected": profile_qout_ia,
                "ia_weekday": profile_qout_weekday_ia,
                "ia_weekend": profile_qout_weekend_ia,
                "real_prev_day": prev_day_qout_real,
                "vertical_lines": chart_qout_vertical_lines,
            },
            "history_recent": {
                "labels": hist_labels,
                "qin": hist_qin,
                "qout": hist_qout,
            },
        },
        "profile_curves": {
            "labels": profile_labels,
            "ia_weekday": profile_qout_weekday_ia,
            "ia_weekend": profile_qout_weekend_ia,
            "real_prev_day": prev_day_qout_real,
        },
        "history_recent": [
            {
                "timestamp": dt.isoformat(timespec="minutes"),
                "qin": qin,
                "qout": qout,
                "vol": vol,
            }
            for dt, qin, vol, qout in recent
        ],
    }


def write_recinto_ia_snapshot(
    snapshot: Dict[str, object],
    max_history: int = 10080,
) -> Optional[Path]:
    if not snapshot or not snapshot.get("recinto"):
        return None

    ensure_dirs()
    recinto = str(snapshot["recinto"])
    out_path = RT3_IA_EXPORT_DIR / f"{recinto}.json"
    history: List[Dict[str, object]] = []
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            raw_history = existing.get("qin_ideal_history") or []
            if isinstance(raw_history, list):
                history = raw_history
        except Exception:
            history = []

    projection = snapshot.get("projection") or {}
    history_entry = {
        "generated_at": snapshot.get("generated_at"),
        "profile_group": snapshot.get("profile_group"),
        "last_qin": ((snapshot.get("summary") or {}).get("last_qin")),
        "last_vol": ((snapshot.get("summary") or {}).get("last_vol")),
        "target_vol": ((snapshot.get("summary") or {}).get("target_vol")),
        "qin_ideal": projection.get("qin_ideal"),
        "projection_start_dt": projection.get("start_dt"),
        "rebalse_with_qin_actual": ((projection.get("rebalse_with_qin_actual") or {}).get("event_dt")),
        "bajo_10_with_qin_actual": ((projection.get("bajo_10_with_qin_actual") or {}).get("event_dt")),
        "rebalse_with_qin_ideal": ((projection.get("rebalse_with_qin_ideal") or {}).get("event_dt")),
        "bajo_10_with_qin_ideal": ((projection.get("bajo_10_with_qin_ideal") or {}).get("event_dt")),
    }
    if history and history[-1].get("generated_at") == history_entry["generated_at"]:
        history[-1] = history_entry
    else:
        history.append(history_entry)
    if len(history) > int(max_history):
        history = history[-int(max_history):]

    payload = {
        "recinto": recinto,
        "updated_at": snapshot.get("generated_at"),
        "snapshot": snapshot,
        "qin_ideal_history": history,
    }
    tmp_path = out_path.with_name(f"{out_path.name}.tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)
    return out_path


def _get_redis_client():
    if redis is None:
        return None
    if not REDIS_HOST:
        return None
    try:
        client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
            password=REDIS_PASSWORD,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
        return client
    except Exception:
        return None


def _ensure_point_exists(point_name: str, descripcion: str) -> None:
    conn = sqlite3.connect(RT3_DB_PATH, timeout=5.0)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA busy_timeout = 5000")
        cur.execute("SELECT id FROM point WHERE tag = ? LIMIT 1", (point_name,))
        row = cur.fetchone()
        if row:
            cur.execute(
                """
                UPDATE point
                SET fuente = COALESCE(NULLIF(fuente, ''), 'ia'),
                    type = COALESCE(NULLIF(type, ''), 'float'),
                    descripcion = COALESCE(NULLIF(descripcion, ''), ?),
                    guardar_historia = COALESCE(NULLIF(guardar_historia, ''), '1')
                WHERE id = ?
                """,
                (descripcion, row[0]),
            )
        else:
            cur.execute(
                """
                INSERT INTO point (tag, fuente, type, descripcion, guardar_historia)
                VALUES (?, 'ia', 'float', ?, '1')
                """,
                (point_name, descripcion),
            )
        conn.commit()
    finally:
        conn.close()


def publish_qin_ideal_points(snapshot: Optional[Dict[str, object]]) -> List[str]:
    """
    Publica qin_ideal en points de salida para consumo externo.
    """
    if not isinstance(snapshot, dict):
        return []
    recinto = str(snapshot.get("recinto") or "").strip()
    if not recinto:
        return []
    projection = snapshot.get("projection") if isinstance(snapshot.get("projection"), dict) else {}
    qin_ideal = projection.get("qin_ideal") if isinstance(projection, dict) else None
    if qin_ideal is None:
        return []
    try:
        qin_value = float(qin_ideal)
    except Exception:
        return []

    # Mantiene compatibilidad con el nombre pedido y con el ejemplo entregado.
    point_names = [
        f"proyeccion.{recinto}",
        f"caudal_ideal.{recinto}",
    ]
    desc = f"Qin ideal calculado por rt3-ia para {recinto}"
    # UTC compacto: point_to_influx interpreta YYYYMMDDHHMMSS de 14 dígitos como UTC.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    for point_name in point_names:
        try:
            _ensure_point_exists(point_name, desc)
        except Exception:
            # Si la BD de definicion de points esta temporalmente bloqueada,
            # igualmente publicar en Redis para no dejar qin_ideal pegado.
            pass

    rds = _get_redis_client()
    if rds is not None:
        for point_name in point_names:
            rds.hset(REDIS_HASH_POINT, point_name, str(qin_value))
            rds.hset(REDIS_HASH_POINT_RAW, point_name, str(qin_value))
            rds.hset(REDIS_HASH_POINT_TS, point_name, timestamp)

    return point_names


def simulate_day_volume_projection_bidirectional(
    anchor_volume: Optional[float],
    qin_fixed: float,
    perfil: List[Dict[str, Optional[float]]],
    qout_key: str,
    anchor_slot: int,
) -> List[Optional[float]]:
    out: List[Optional[float]] = [None for _ in perfil]
    if anchor_volume is None or not perfil:
        return out
    safe_slot = max(0, min(len(perfil) - 1, anchor_slot))
    out[safe_slot] = max(0.0, float(anchor_volume))

    vol = max(0.0, float(anchor_volume))
    for slot in range(safe_slot + 1, len(perfil)):
        qout = perfil[slot].get(qout_key)
        qout_val = float(qout) if qout is not None else 0.0
        vol = max(0.0, vol + ((float(qin_fixed) - qout_val) * 900.0 / 1000.0))
        out[slot] = vol

    vol = max(0.0, float(anchor_volume))
    for slot in range(safe_slot - 1, -1, -1):
        next_slot = slot + 1
        qout = perfil[next_slot].get(qout_key)
        qout_val = float(qout) if qout is not None else 0.0
        vol = max(0.0, vol - ((float(qin_fixed) - qout_val) * 900.0 / 1000.0))
        out[slot] = vol

    return out


def build_ideal_volume_series_from_consumption(
    perfil: List[Dict[str, Optional[float]]],
    qout_key: str,
    vol_min: Optional[float],
    vol_max: Optional[float],
) -> List[Optional[float]]:
    if not perfil or vol_min is None or vol_max is None:
        return [None for _ in perfil]

    qout_values = [float(row[qout_key]) for row in perfil if row.get(qout_key) is not None]
    if not qout_values:
        return [None for _ in perfil]

    qout_min = min(qout_values)
    qout_max = max(qout_values)
    if qout_max <= qout_min:
        midpoint = (float(vol_min) + float(vol_max)) / 2.0
        return [midpoint for _ in perfil]

    out: List[Optional[float]] = []
    for row in perfil:
        qout = row.get(qout_key)
        if qout is None:
            out.append(None)
            continue
        ratio = (float(qout) - qout_min) / (qout_max - qout_min)
        ideal_vol = float(vol_min) + ratio * (float(vol_max) - float(vol_min))
        out.append(ideal_vol)
    return out


def build_sinusoidal_ideal_volume_series(
    perfil: List[Dict[str, Optional[float]]],
    qout_key: str,
    vol_min: Optional[float],
    vol_max: Optional[float],
) -> List[Optional[float]]:
    if not perfil or vol_min is None or vol_max is None:
        return [None for _ in perfil]

    # Construir una señal diaria robusta por slot (0..95), para que
    # la curva ideal quede alineada en "Hoy" y en "7 dias anterior".
    slot_samples: Dict[int, List[float]] = {}
    fallback_samples: List[Tuple[int, float]] = []
    for idx, row in enumerate(perfil):
        value = row.get(qout_key)
        if value is None:
            continue
        try:
            v = float(value)
        except Exception:
            continue
        slot_raw = row.get("slot_index")
        try:
            slot = int(slot_raw) % 96
            slot_samples.setdefault(slot, []).append(v)
        except Exception:
            fallback_samples.append((idx % 96, v))

    qout_by_slot: Dict[int, float] = {}
    if slot_samples:
        for slot, values in slot_samples.items():
            if values:
                qout_by_slot[slot] = sum(values) / len(values)
    elif fallback_samples:
        for slot, v in fallback_samples:
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

    # El umbral maximo del volumen ideal debe quedar entre la fecha del consumo minimo
    # y la fecha del consumo maximo. Tomamos el punto medio de ese tramo circular diario.
    span = (max_idx - min_idx) % 96
    phase = (min_idx + (span / 2.0)) % 96

    out: List[Optional[float]] = []
    for idx, row in enumerate(perfil):
        slot_raw = row.get("slot_index")
        try:
            daily_idx = int(slot_raw) % 96
        except Exception:
            daily_idx = idx % 96
        value = center + amplitude * math.cos(omega * (daily_idx - phase))
        out.append(max(float(vol_min), min(float(vol_max), value)))
    return out


def build_extended_profile_window(
    perfil: List[Dict[str, Optional[float]]],
    start_day: datetime,
    extra_hours: int = 8,
) -> Tuple[List[str], List[str], List[Dict[str, Optional[float]]]]:
    if not perfil:
        return [], [], []
    extra_slots = max(0, int(extra_hours * 4))
    total_slots = len(perfil) + extra_slots + 1
    labels: List[str] = []
    tooltip_dates: List[str] = []
    rows: List[Dict[str, Optional[float]]] = []
    for idx in range(total_slots):
        ts = start_day + timedelta(minutes=15 * idx)
        slot = idx % len(perfil)
        base = perfil[slot]
        labels.append(ts.strftime("%H:%M"))
        tooltip_dates.append(ts.strftime("%Y-%m-%d %H:%M"))
        rows.append(
            {
                "slot_index": base.get("slot_index"),
                "hora_texto": ts.strftime("%H:%M"),
                "qout_promedio": base.get("qout_promedio"),
                "qout_ia": base.get("qout_ia"),
            }
        )
    return labels, tooltip_dates, rows


SPANISH_MONTH_ABBR = {
    1: "ene",
    2: "feb",
    3: "mar",
    4: "abr",
    5: "may",
    6: "jun",
    7: "jul",
    8: "ago",
    9: "sep",
    10: "oct",
    11: "nov",
    12: "dic",
}


def format_day_label(dt: datetime) -> str:
    return f"{dt.day:02d}/{SPANISH_MONTH_ABBR.get(dt.month, dt.month)}"


# ---------------------------------------------------------------------------
# Lógica de lectura MySQL + promedios 15 min con interpolación
# ---------------------------------------------------------------------------


def fetch_raw_values_mysql(
    tagid: int, t_ini: datetime, t_fin: datetime
) -> List[Tuple[datetime, float]]:
    """
    Lee desde MySQL legacy:
      SELECT fecha, valor FROM rtdata.tag_{tagid}
      WHERE fecha BETWEEN t_ini AND t_fin
      ORDER BY fecha;
    """
    conn = None
    try:
        conn = get_mysql_connection()
        cur = conn.cursor()
        table = f"tag_{int(tagid)}"
        sql = f"""
            SELECT fecha, valor
            FROM {table}
            WHERE fecha >= %s AND fecha <= %s
            ORDER BY fecha
        """
        cur.execute(sql, (t_ini, t_fin))
        out: List[Tuple[datetime, float]] = []
        for fecha, valor in cur.fetchall():
            if fecha is None or valor is None:
                continue
            if not isinstance(fecha, datetime):
                # mysql-connector puede devolver date/datetime; forzamos datetime
                fecha = datetime.fromisoformat(str(fecha))
            out.append((fecha, float(valor)))
        return out
    finally:
        if conn is not None:
            conn.close()


def fetch_raw_values_influx(
    point: str, t_ini: datetime, t_fin: datetime
) -> List[Tuple[datetime, float]]:
    if not point:
        return []
    if t_ini is None or t_fin is None or t_ini >= t_fin:
        return []
    try:
        from influxdb_client import InfluxDBClient
    except Exception:
        return []

    def _flux_escape_string(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _flux_time_literal(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=CHILE_TZ)
        else:
            value = value.astimezone(timezone.utc)
        value = value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")

    measurement = str(point).strip()
    measurement_escaped = _flux_escape_string(measurement)
    # Traemos contexto antes y despues del rango para poder interpolar
    # huecos al borde cuando el point en Influx es esparso.
    query_start = t_ini - timedelta(hours=24)
    query_end = t_fin + timedelta(hours=24)
    t_ini_flux = _flux_time_literal(query_start)
    t_fin_flux = _flux_time_literal(query_end)
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    try:
        query_api = client.query_api()
        query = (
            f'from(bucket: "{INFLUX_BUCKET}")\n'
            f'  |> range(start: time(v: "{t_ini_flux}"), stop: time(v: "{t_fin_flux}"))\n'
            f'  |> filter(fn: (r) => r._measurement == "{measurement_escaped}")\n'
            '  |> filter(fn: (r) => r._field == "valor")\n'
            '  |> sort(columns: ["_time"], desc: false)\n'
            '  |> limit(n: 200000)\n'
        )
        result = query_api.query(query=query, org=INFLUX_ORG)
        out: List[Tuple[datetime, float]] = []
        for table in result:
            for record in table.records:
                dt = record.get_time()
                value = record.get_value()
                if dt is None or value is None:
                    continue
                if dt.tzinfo is not None:
                    dt_local = dt.astimezone(CHILE_TZ).replace(tzinfo=None)
                else:
                    dt_local = dt
                out.append((dt_local, float(value)))
        return out
    finally:
        try:
            client.close()
        except Exception:
            pass


def fetch_raw_values(
    source_type: str,
    tagid: Optional[int],
    point: Optional[str],
    t_ini: datetime,
    t_fin: datetime,
) -> List[Tuple[datetime, float]]:
    if point:
        return fetch_raw_values_influx(point, t_ini, t_fin)
    if tagid:
        return fetch_raw_values_mysql(int(tagid), t_ini, t_fin)
    return []


def build_time_bins(
    t_ini: datetime, t_fin: datetime, step_minutes: int = 15
) -> List[datetime]:
    """
    Genera instantes cada 15 minutos desde t_ini hasta t_fin (incluido t_fin).
    Los bordes se redondean a múltiplos de 15 minutos.
    """
    # redondear inicio hacia abajo y fin hacia arriba a múltiplos de 15 minutos
    def round_down_15(dt: datetime) -> datetime:
        m = (dt.minute // step_minutes) * step_minutes
        return dt.replace(minute=m, second=0, microsecond=0)

    def round_up_15(dt: datetime) -> datetime:
        m = ((dt.minute + step_minutes - 1) // step_minutes) * step_minutes
        if m >= 60:
            dt = dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            dt = dt.replace(minute=m, second=0, microsecond=0)
        return dt

    start = round_down_15(t_ini)
    end = round_up_15(t_fin)
    bins: List[datetime] = []
    cur = start
    while cur <= end:
        bins.append(cur)
        cur += timedelta(minutes=step_minutes)
    return bins


def compute_bin_averages(
    raw: List[Tuple[datetime, float]],
    bins: List[datetime],
    step_minutes: int = 15,
) -> Dict[datetime, Optional[float]]:
    """
    Calcula mediana dentro de cada ventana [t, t+15min).
    Devuelve dict: instante_bin -> mediana o None si no hay datos.
    """
    if not raw:
        return {b: None for b in bins}

    bin_size = timedelta(minutes=step_minutes)
    # índice sobre los datos crudos
    idx = 0
    n = len(raw)
    result: Dict[datetime, Optional[float]] = {}

    for b in bins:
        b_end = b + bin_size
        values: List[float] = []

        # avanzar hasta que la fecha esté dentro de la ventana
        while idx < n and raw[idx][0] < b:
            idx += 1

        j = idx
        while j < n and raw[j][0] < b_end:
            values.append(raw[j][1])
            j += 1

        if values:
            result[b] = float(statistics.median(values))
        else:
            result[b] = None

    return result


def interpolate_missing(
    series: Dict[datetime, Optional[float]]
) -> Dict[datetime, Optional[float]]:
    """
    Interpola valores faltantes:
    - Si hay valor anterior y siguiente, interpolación lineal.
    - Si sólo hay anterior, se mantiene (proyección).
    - Si sólo hay siguiente, se usa el siguiente.
    """
    times = sorted(series.keys())
    values = [series[t] for t in times]

    # posiciones de valores conocidos
    known_indices = [i for i, v in enumerate(values) if v is not None]
    if not known_indices:
        # no hay nada que interpolar, devolvemos todo None
        return series

    # rellenar hacia la izquierda
    first_known = known_indices[0]
    for i in range(0, first_known):
        values[i] = values[first_known]

    # rellenar hacia la derecha
    last_known = known_indices[-1]
    for i in range(last_known + 1, len(values)):
        values[i] = values[last_known]

    # interpolar dentro de los huecos
    prev_idx = first_known
    for i in range(first_known + 1, len(values)):
        if values[i] is not None:
            # tenemos siguiente conocido
            start_idx = prev_idx
            end_idx = i
            if end_idx - start_idx > 1:
                v0 = values[start_idx]
                v1 = values[end_idx]
                dt_total = (times[end_idx] - times[start_idx]).total_seconds()
                for k in range(start_idx + 1, end_idx):
                    alpha = (times[k] - times[start_idx]).total_seconds() / dt_total
                    values[k] = v0 + (v1 - v0) * alpha
            prev_idx = i

    # reconstruir dict
    out: Dict[datetime, Optional[float]] = {}
    for t, v in zip(times, values):
        out[t] = v
    return out


def calcular_series_recinto(
    cfg: RecintoConfig, t_ini: datetime, t_fin: datetime, paso_min: int = 15
) -> List[Tuple[datetime, float, float, float]]:
    """
    Calcula series qin, vol, qout con promedios e interpolación.
    """
    bins = build_time_bins(t_ini, t_fin, paso_min)

    raw_qin1 = fetch_raw_values(cfg.source_type, cfg.tag_qin1, cfg.point_qin1, t_ini, t_fin)
    if (cfg.source_type == "influxdb" and cfg.point_qin2) or (cfg.tag_qin2 is not None and int(cfg.tag_qin2) > 0):
        raw_qin2 = fetch_raw_values(cfg.source_type, cfg.tag_qin2, cfg.point_qin2, t_ini, t_fin)
        qin2_present = True
    else:
        raw_qin2 = []
        qin2_present = False

    raw_vol = fetch_raw_values(cfg.source_type, cfg.tag_vol, cfg.point_vol, t_ini, t_fin)
    m3hr_args = parse_m3hr_expression(cfg.point_qout)
    if m3hr_args:
        if len(m3hr_args) >= 2:
            vol_expr = m3hr_args[0]
            inflow_expr = m3hr_args[1]
            vol_tagid_expr, vol_point_expr, vol_source_expr = infer_source_from_field(vol_expr)
            raw_vol = fetch_raw_values(vol_source_expr, vol_tagid_expr, vol_point_expr, t_ini, t_fin)
        else:
            inflow_expr = m3hr_args[0]
        inflow_tagid, inflow_point, inflow_source = infer_source_from_field(inflow_expr)
        raw_qout = fetch_raw_values(inflow_source, inflow_tagid, inflow_point, t_ini, t_fin)
    else:
        raw_qout = fetch_raw_values(cfg.source_type, cfg.tag_qout, cfg.point_qout, t_ini, t_fin)
        # Convertir a l/s si el usuario configura qout en m3/hr
        if cfg.qout_unit == "m3/hr":
            raw_qout = [(dt, float(v) / 3.6) for dt, v in raw_qout]

    avg_qin1 = compute_bin_averages(raw_qin1, bins, paso_min)
    if qin2_present:
        avg_qin2 = compute_bin_averages(raw_qin2, bins, paso_min)
    else:
        # si no existe qin2, lo dejamos en None para no forzar qin=0 artificialmente
        avg_qin2 = {b: None for b in bins}

    avg_vol = compute_bin_averages(raw_vol, bins, paso_min)
    avg_qout = compute_bin_averages(raw_qout, bins, paso_min)

    interp_qin1 = interpolate_missing(avg_qin1)
    interp_qin2 = interpolate_missing(avg_qin2) if qin2_present else avg_qin2
    interp_vol = interpolate_missing(avg_vol)
    interp_qout = interpolate_missing(avg_qout)

    if m3hr_args:
        steps_back = max(int(round(60 / max(paso_min, 1))), 1)
        qout_from_m3hr: Dict[datetime, Optional[float]] = {}
        for idx, b in enumerate(bins):
            vol_now = interp_vol.get(b)
            past_idx = max(0, idx - steps_back)
            vol_prev = interp_vol.get(bins[past_idx]) if bins else None
            if vol_now is None or vol_prev is None:
                qout_from_m3hr[b] = None
                continue

            inflow_m3 = 0.0
            start_idx = max(0, idx - steps_back + 1)
            for j in range(start_idx, idx + 1):
                inflow_lps = interp_qout.get(bins[j])
                if inflow_lps is None:
                    continue
                inflow_m3 += float(inflow_lps) * (paso_min * 60.0) / 1000.0

            consumo_m3hr = max((float(vol_prev) - float(vol_now)) + inflow_m3, 0.0)
            qout_from_m3hr[b] = consumo_m3hr / 3.6

        interp_qout = interpolate_missing(qout_from_m3hr)

    result: List[Tuple[datetime, float, float, float]] = []
    for b in bins:
        v1 = interp_qin1.get(b)
        v2 = interp_qin2.get(b)
        if v1 is None and v2 is None:
            qin_val = None
        else:
            qin_val = (float(v1) if v1 is not None else 0.0) + (float(v2) if v2 is not None else 0.0)
        result.append(
            (
                b,
                qin_val,
                float(interp_vol[b]) if interp_vol[b] is not None else None,
                float(interp_qout[b]) if interp_qout[b] is not None else None,
            )
        )

    return result


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------


app = Flask(__name__)


INDEX_TEMPLATE = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>rt3-ia Recintos</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; background: #f5f5f5; color: #222; }
    .top-menu {
      width: 100%;
      background: #000;
      color: #fff;
      padding: 0.45rem 1rem;
      box-sizing: border-box;
    }
    .top-menu ul { list-style:none; margin:0; padding:0; }
    .top-menu > ul > li {
      display:inline-block;
      position:relative;
      margin-right:0.5rem;
    }
    .top-menu a, .top-menu .menu-label {
      display:block;
      color:#e5e7eb;
      text-decoration:none;
      padding:0.35rem 0.55rem;
      background:#111827;
      border:1px solid #374151;
      border-radius:4px;
      font-size:0.86rem;
      cursor:default;
    }
    .top-menu a:hover { background:#1f2937; }
    .top-menu li ul {
      display:none;
      position:absolute;
      left:0;
      top:100%;
      min-width:220px;
      z-index:20;
      padding-top:0.2rem;
    }
    .top-menu li:hover > ul { display:block; }
    .top-menu li ul li { position:relative; }
    .top-menu li ul li ul {
      left:100%;
      top:0;
      padding-left:0.2rem;
      padding-top:0;
    }
    .page-wrap { padding: 1.2rem; }
    h1 { color: #333; }
    form { background: #fff; padding: 1.5rem; border-radius: 8px; max-width: 480px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
    label { display: block; margin-top: 0.5rem; font-weight: bold; }
    input[type=text], input[type=number], input[type=datetime-local] {
      width: 100%; padding: 0.4rem 0.6rem; margin-top: 0.2rem; box-sizing: border-box;
      background: #fff;
      border: 1px solid #bdbdbd;
      border-radius: 4px;
    }
    button { margin-top: 1rem; padding: 0.5rem 1.2rem; background:#1976d2; color:#fff;
             border:none; border-radius:4px; cursor:pointer; }
    button:hover { background:#145ea8; }
    .note { margin-top: 1rem; font-size: 0.9rem; color:#555; }
    .table-wrap {
      background: #fff;
      border: 1px solid #d6d6d6;
      border-radius: 8px;
      overflow-x: auto;
      margin-bottom: 1.5rem;
      box-shadow: 0 1px 4px rgba(0,0,0,0.05);
    }

    .recintos-table {
      width: 100%;
      min-width: 1120px;
      font-size: 0.78rem;
      line-height: 1.15;
      border-collapse: collapse;
      table-layout: auto;
    }
    .recintos-table th,
    .recintos-table td {
      padding: 0.35rem 0.45rem;
      white-space: nowrap;
      border: 1px solid #d5d5d5;
      vertical-align: middle;
    }
    .recintos-table th {
      background: #e9e9e9;
      position: sticky;
      top: 0;
      z-index: 1;
    }
    .recintos-table td:nth-child(1) { font-weight: 600; }
    .recintos-table td:nth-child(8),
    .recintos-table td:nth-child(9),
    .recintos-table td:nth-child(10) {
      font-family: monospace;
      font-size: 0.73rem;
    }
    .date-fresh { color: #2e7d32; font-weight: 700; }
    .date-stale { color: #ef6c00; font-weight: 700; }
    .recintos-table button {
      margin-top: 0;
      padding: 0.2rem 0.45rem;
      font-size: 0.7rem;
      border-radius: 3px;
      line-height: 1.1;
      min-height: 24px;
      height: 24px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      white-space: nowrap;
    }
    /* Botones simples grises dentro de la tabla */
    .recintos-table button {
      background: #9e9e9e !important;
      color: #111 !important;
    }
    .recintos-table button:hover {
      background: #757575 !important;
    }
    .actions-cell { display: flex; flex-wrap: nowrap; gap: 0.25rem; align-items: center; white-space: nowrap; }
    .actions-cell form { margin: 0; padding: 0; background: transparent; box-shadow: none; border-radius: 0; max-width: none; }
    .recintos-table td:last-child { vertical-align: middle; min-width: 180px; }
    @media (max-width: 1200px) {
      body { margin: 0.9rem; }
      .recintos-table { font-size: 0.74rem; }
      .recintos-table th,
      .recintos-table td { padding: 0.3rem 0.35rem; }
    }
    @media (max-width: 860px) {
      .recintos-table,
      .recintos-table thead,
      .recintos-table tbody,
      .recintos-table tr,
      .recintos-table th,
      .recintos-table td {
        display: block;
        width: 100%;
      }
      .recintos-table thead {
        display: none;
      }
      .recintos-table tr {
        border-bottom: 1px solid #d5d5d5;
        padding: 0.35rem 0;
      }
      .recintos-table td {
        border: none;
        padding: 0.18rem 0.45rem;
      }
      .recintos-table td:last-child {
        margin-top: 0.25rem;
        padding-top: 0.35rem;
        border-top: 1px solid #e3e3e3;
        min-width: 0;
      }
      .actions-cell {
        justify-content: flex-start;
        overflow-x: auto;
        padding-bottom: 0.1rem;
      }
    }
    .suggest-wrap { position: relative; }
    .suggest-box { position:absolute; left:0; right:0; top:100%; background:#fff; border:1px solid #cfcfcf; border-top:none; z-index:10; max-height:180px; overflow:auto; display:none; }
    .suggest-item { padding:0.35rem 0.55rem; cursor:pointer; font-size:0.82rem; }
    .suggest-item:hover { background:#eef5ff; }
  </style>
</head>
<body>
  <nav class="top-menu">
    <ul>
      <li>
        <span class="menu-label">menu</span>
        <ul>
          <li><a href="{{ url_for('index') }}">proyeccion</a></li>
          <li>
            <a href="{{ url_for('correlacion_index') }}">correlacion</a>
            <ul>
              <li><a href="{{ url_for('correlacion_index') }}">lista de proyecto</a></li>
              <li><a href="{{ url_for('correlacion_index') }}#ingresar-tags">ingresar tags</a></li>
              <li><a href="{{ url_for('correlacion_results_index') }}">ver resultados</a></li>
            </ul>
          </li>
        </ul>
      </li>
    </ul>
  </nav>
  <div class="page-wrap">
  <h1>Configurar recinto / estanque</h1>

  <h2>Recintos configurados</h2>
  <div class="table-wrap">
  <table class="recintos-table" border="0" cellpadding="0" cellspacing="0">
    <thead style="background:#e0e0e0;">
      <tr>
        <th>Nombre</th>
        <th>point_qin1</th>
        <th>point_qin2</th>
        <th>point_vol</th>
        <th>point_qout</th>
        <th>Fuente</th>
        <th>Vol máx estanque</th>
        <th>Unidad qout</th>
        <th>Fecha ini SQLite</th>
        <th>Fecha fin SQLite</th>
        <th>Última migración</th>
        <th>IA < 1 día</th>
        <th>Registros en SQLite</th>
        <th>Estado</th>
        <th>Acciones</th>
      </tr>
    </thead>
    <tbody>
    {% if recintos %}
      {% for r in recintos %}
      <tr>
        {% set form_id = 'edit_form_' ~ loop.index %}
        <td>
          {% if editing_name == r.nombre %}
            {{ r.nombre }}
          {% else %}
            <a href="{{ url_for('index', edit=r.nombre) }}" style="color:#0d47a1; text-decoration:underline;">
              {{ r.nombre }}
            </a>
          {% endif %}
        </td>
        {% if editing_name == r.nombre %}
          <td>
            <div class="suggest-wrap">
              <input class="point-source-input" style="width:170px;" type="text" name="point_qin1" value="{{ r.point_qin1 or (r.tag_qin1 if r.tag_qin1 is not none else '') }}" form="{{ form_id }}" autocomplete="off">
              <div class="suggest-box"></div>
            </div>
          </td>
          <td>
            <div class="suggest-wrap">
              <input class="point-source-input" style="width:170px;" type="text" name="point_qin2" value="{{ r.point_qin2 or (r.tag_qin2 if r.tag_qin2 is not none else '') }}" form="{{ form_id }}" autocomplete="off">
              <div class="suggest-box"></div>
            </div>
          </td>
          <td>
            <div class="suggest-wrap">
              <input class="point-source-input" style="width:170px;" type="text" name="point_vol" value="{{ r.point_vol or (r.tag_vol if r.tag_vol is not none else '') }}" form="{{ form_id }}" autocomplete="off">
              <div class="suggest-box"></div>
            </div>
          </td>
          <td>
            <div class="suggest-wrap">
              <input class="point-source-input" style="width:170px;" type="text" name="point_qout" value="{{ r.point_qout or (r.tag_qout if r.tag_qout is not none else '') }}" form="{{ form_id }}" autocomplete="off">
              <div class="suggest-box"></div>
            </div>
          </td>
          <td>{{ "auto" }}</td>
          <td><input style="width:110px;" type="number" step="0.01" name="volumen_maximo" value="{{ r.volumen_maximo if r.volumen_maximo is not none else '' }}" form="{{ form_id }}"></td>
          <td>
            <select name="qout_unit" form="{{ form_id }}" style="width:110px;">
              <option value="l/s" {% if (r.qout_unit or 'l/s') == 'l/s' %}selected{% endif %}>l/s</option>
              <option value="m3/hr" {% if (r.qout_unit or 'l/s') == 'm3/hr' %}selected{% endif %}>m3/hr</option>
            </select>
          </td>
          <td>{{ r.fecha_ini_sqlite or "-" }}</td>
          <td class="{{ 'date-' + r.fecha_fin_estado if r.fecha_fin_estado != 'unknown' else '' }}">
            {{ r.fecha_fin_sqlite or "-" }}
          </td>
          <td>{{ r.last_run_at or "-" }}</td>
          <td style="text-align:center;">{{ "✓" if r.ia_recent else "" }}</td>
          <td>{{ r.total_rows }}</td>
          <td>
            <label style="display:flex; align-items:center; gap:6px; justify-content:center;">
              <input type="checkbox" name="activo" value="1" {% if r.activo %}checked{% endif %} form="{{ form_id }}">
              Activo
            </label>
          </td>
          <td>
            <div class="actions-cell">
              <form method="post" id="{{ form_id }}" action="{{ url_for('editar_recinto_config', nombre=r.nombre) }}">
                <button type="submit" style="background:#388e3c;">Guardar</button>
                <a href="{{ url_for('index') }}" style="margin-left:6px; background:#9e9e9e; padding:0.2rem 0.45rem; border-radius:3px; color:#111; text-decoration:none; display:inline-block; line-height:30px; height:30px;">
                  cancelar
                </a>
              </form>
            </div>
          </td>
        {% else %}
          <td>{{ r.point_qin1 or (r.tag_qin1 if r.tag_qin1 is not none else "-") }}</td>
          <td>{{ r.point_qin2 or (r.tag_qin2 if r.tag_qin2 is not none else "-") }}</td>
          <td>{{ r.point_vol or (r.tag_vol if r.tag_vol is not none else "-") }}</td>
          <td>{{ r.point_qout or (r.tag_qout if r.tag_qout is not none else "-") }}</td>
          <td>{{ r.source_type }}</td>
          <td>{{ "%.2f"|format(r.volumen_maximo) if r.volumen_maximo is not none else "-" }}</td>
          <td>{{ r.qout_unit or 'l/s' }}</td>
          <td>{{ r.fecha_ini_sqlite or "-" }}</td>
        <td class="{{ 'date-' + r.fecha_fin_estado if r.fecha_fin_estado != 'unknown' else '' }}">{{ r.fecha_fin_sqlite or "-" }}</td>
          <td>{{ r.last_run_at or "-" }}</td>
          <td style="text-align:center;">{{ "✓" if r.ia_recent else "" }}</td>
          <td>{{ r.total_rows }}</td>
          <td>{{ "Activo" if r.activo else "Inactivo" }}</td>
          <td>
            <div class="actions-cell">
              <form method="get" action="{{ url_for('ver_recinto', nombre=r.nombre) }}">
                <button type="submit" style="background:#9e9e9e;">tabla</button>
              </form>
              <form method="get" action="{{ url_for('ver_recinto_ia', nombre=r.nombre) }}">
                <button type="submit" style="background:#9e9e9e;">IA</button>
              </form>
              <form method="post" action="{{ url_for('limpiar_recinto_sqlite', nombre=r.nombre) }}" onsubmit="return confirm('¿Borrar toda la info del SQLite de {{ r.nombre }}?');">
                <input type="hidden" name="paso_min" value="15">
                <input type="hidden" name="t_ini" value="{{ default_t_ini }}">
                <input type="hidden" name="t_fin" value="{{ default_t_fin }}">
                <button type="submit" style="background:#9e9e9e;">full</button>
              </form>
              <form method="post" action="{{ url_for('append_recinto', nombre=r.nombre) }}">
                <input type="hidden" name="paso_min" value="15">
                <button type="submit" style="background:#9e9e9e;">append</button>
              </form>
              <form method="post" action="{{ url_for('toggle_recinto', nombre=r.nombre) }}">
                <input type="hidden" name="activo" value="{{ 0 if r.activo else 1 }}">
                <button type="submit" style="background:#9e9e9e;">{{ "Desactivar" if r.activo else "Activar" }}</button>
              </form>
              <form method="post" action="{{ url_for('eliminar_recinto', nombre=r.nombre) }}" onsubmit="return confirm('¿Eliminar recinto {{ r.nombre }}?');">
                <button type="submit" style="background:#9e9e9e;">Eliminar</button>
              </form>
            </div>
          </td>
        {% endif %}
      </tr>
      {% endfor %}
    {% else %}
      <tr><td colspan="14">No hay recintos configurados.</td></tr>
    {% endif %}
    </tbody>
  </table>
  </div>

  {% if status_msg %}
  <div style="background:#e3f2fd;border:1px solid #90caf9;padding:0.8rem;border-radius:4px;margin-bottom:1rem;">
    {{ status_msg }}
  </div>
  {% endif %}

  <h2>Agregar / actualizar recinto</h2>
  <form method="post" action="{{ url_for('crear_recinto') }}">
    <label>Nombre recinto (ej: tk_copa)</label>
    <input type="text" name="nombre" required>

    <label>Point caudal entrada (qin1)</label>
    <div class="suggest-wrap">
      <input class="point-source-input" type="text" name="point_qin1" required autocomplete="off">
      <div class="suggest-box"></div>
    </div>

    <label>Point caudal entrada (qin2)</label>
    <div class="suggest-wrap">
      <input class="point-source-input" type="text" name="point_qin2" autocomplete="off">
      <div class="suggest-box"></div>
    </div>

    <label>Point volumen (vol)</label>
    <div class="suggest-wrap">
      <input class="point-source-input" type="text" name="point_vol" required autocomplete="off">
      <div class="suggest-box"></div>
    </div>

    <label>Point caudal salida (qout)</label>
    <div class="suggest-wrap">
      <input class="point-source-input" type="text" name="point_qout" required autocomplete="off">
      <div class="suggest-box"></div>
    </div>

    <label>Volumen máximo estanque (m3)</label>
    <input type="number" step="0.01" name="volumen_maximo">

    <label>Unidad caudal salida (qout): l/s o m3/hr</label>
    <select name="qout_unit">
      <option value="l/s" selected>L/s</option>
      <option value="m3/hr">m3/hr</option>
    </select>

    <label>Fecha inicio</label>
    <input type="datetime-local" name="t_ini" required value="{{ default_t_ini }}">

    <label>Fecha fin</label>
    <input type="datetime-local" name="t_fin" required value="{{ default_t_fin }}">

    <label>Paso (minutos, default 15)</label>
    <input type="number" name="paso_min" value="15" min="1">

    <button type="submit">Guardar y procesar (recalcular todo)</button>

    <p class="note">
      Esto guardará la configuración del recinto y calculará promedios cada 15 minutos
      desde MySQL legacy, almacenándolos en un SQLite llamado &lt;recinto&gt;.sqlite
      con la tabla <code>medidas(fecha, qin, vol, qout)</code>.
    </p>
  </form>
  <script>
    async function wirePointSuggestions(root) {
      const inputs = root.querySelectorAll('.point-source-input');
      inputs.forEach(input => {
        const box = input.parentElement.querySelector('.suggest-box');
        if (!box) return;
        input.addEventListener('input', async () => {
          const q = input.value.trim();
          if (!q || /^[0-9]+$/.test(q) || q.length < 2) {
            box.style.display = 'none';
            box.innerHTML = '';
            return;
          }
          const res = await fetch('/recintos/api/point-suggest?q=' + encodeURIComponent(q));
          const data = await res.json();
          box.innerHTML = (data.items || []).map(item => '<div class="suggest-item" data-tag="' + item.tag + '">' + item.tag + (item.descripcion ? ' | ' + item.descripcion : '') + (item.source ? ' [' + item.source + ']' : '') + '</div>').join('');
          box.style.display = box.innerHTML ? 'block' : 'none';
          box.querySelectorAll('.suggest-item').forEach(el => {
            el.addEventListener('mousedown', (ev) => {
              ev.preventDefault();
              input.value = el.dataset.tag || '';
              box.style.display = 'none';
              box.innerHTML = '';
            });
          });
        });
        input.addEventListener('blur', () => setTimeout(() => { box.style.display = 'none'; }, 120));
      });
    }
    wirePointSuggestions(document);
  </script>
</div>
</body>
</html>
"""


CORR_TEMPLATE = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>rt3-ia Analisis de correlacion</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 1.2rem; background: #f5f5f5; color: #222; }
    .card { background:#fff; border:1px solid #d6d6d6; border-radius:8px; padding:1rem; margin-bottom:1rem; }
    .row { display:grid; grid-template-columns: 220px 1fr; gap:0.6rem; margin-bottom:0.5rem; align-items:center; }
    input, select, button { padding:0.35rem 0.5rem; }
    table { width:100%; border-collapse:collapse; background:#fff; }
    th, td { border:1px solid #ddd; padding:0.4rem; font-size:0.85rem; }
    th { background:#eee; }
    .btn { background:#1976d2; color:#fff; border:none; border-radius:4px; cursor:pointer; }
    .btn.gray { background:#757575; }
    .btn.green { background:#2e7d32; }
  </style>
</head>
<body>
  <div style="margin-bottom:0.8rem;">
    <a href="{{ url_for('index') }}" style="display:inline-block;padding:0.4rem 0.8rem;background:#111;color:#fff;border-radius:4px;text-decoration:none;">Volver a recintos</a>
  </div>
  <h1>Analisis de correlacion</h1>

  {% if status_msg %}
  <div class="card" style="border-color:#90caf9;background:#e3f2fd;">{{ status_msg }}</div>
  {% endif %}

  <div class="card" id="ingresar-tags">
    <h3>Crear / editar proyecto</h3>
    <form method="post" action="{{ url_for('correlacion_save_project') }}">
      <div class="row">
        <label>Nombre proyecto</label>
        <input type="text" name="nombre" required value="{{ active_project.nombre if active_project else '' }}">
      </div>
      <div class="row">
        <label>Fecha inicio</label>
        <input type="datetime-local" name="fecha_ini" required value="{{ active_project.fecha_ini if active_project else default_t_ini }}">
      </div>
      <div class="row">
        <label>Fecha fin</label>
        <input type="datetime-local" name="fecha_fin" required value="{{ active_project.fecha_fin if active_project else default_t_fin }}">
      </div>
      <table id="tags-table">
        <thead><tr><th>tagid</th><th>codigo_tag</th><th>tipo medidor</th><th></th></tr></thead>
        <tbody id="tags-body">
          {% if active_project and active_project.tags %}
            {% for t in active_project.tags %}
            <tr>
              <td><input type="number" name="tagid[]" required value="{{ t.tagid }}"></td>
              <td><input type="text" name="codigo_tag[]" value="{{ t.codigo_tag }}" readonly tabindex="-1" style="background:#f3f4f6;"></td>
              <td>
                <select name="medidor_tipo[]">
                  <option value="caudal_l_s" {% if t.medidor_tipo=='caudal_l_s' %}selected{% endif %}>caudal l/s</option>
                  <option value="m3_hr" {% if t.medidor_tipo=='m3_hr' %}selected{% endif %}>m3/hr</option>
                  <option value="volumen_m3" {% if t.medidor_tipo=='volumen_m3' %}selected{% endif %}>volumen m3</option>
                  <option value="cm" {% if t.medidor_tipo=='cm' %}selected{% endif %}>cm</option>
                  <option value="presion" {% if t.medidor_tipo=='presion' %}selected{% endif %}>presion</option>
                  <option value="otro" {% if t.medidor_tipo=='otro' %}selected{% endif %}>otro</option>
                </select>
              </td>
              <td><button type="button" class="btn gray" onclick="this.closest('tr').remove()">x</button></td>
            </tr>
            {% endfor %}
          {% else %}
            <tr>
              <td><input type="number" name="tagid[]" required></td>
              <td><input type="text" name="codigo_tag[]" readonly tabindex="-1" style="background:#f3f4f6;"></td>
              <td>
                <select name="medidor_tipo[]">
                  <option value="caudal_l_s">caudal l/s</option>
                  <option value="m3_hr">m3/hr</option>
                  <option value="volumen_m3">volumen m3</option>
                  <option value="cm">cm</option>
                  <option value="presion">presion</option>
                  <option value="otro">otro</option>
                </select>
              </td>
              <td><button type="button" class="btn gray" onclick="this.closest('tr').remove()">x</button></td>
            </tr>
          {% endif %}
        </tbody>
      </table>
      <div style="margin-top:0.5rem;">
        <button class="btn gray" type="button" onclick="addRow()">Agregar tag</button>
        <button class="btn" type="submit">Guardar proyecto</button>
      </div>
    </form>
  </div>

  <!-- Se omite tabla de universo de tags para no saturar la vista -->

  <div class="card">
    <h3>Proyectos guardados</h3>
    <table>
      <thead><tr><th>nombre proyecto</th><th>acciones</th></tr></thead>
      <tbody>
        {% for p in projects %}
        <tr>
          <td>{{ p.nombre }}</td>
          <td>
            <a class="btn gray" href="{{ url_for('correlacion_index', project=p.nombre) }}">Abrir</a>
            <a class="btn gray" href="{{ url_for('correlacion_results_index', project=p.nombre) }}">Resultados</a>
            <form method="post" action="{{ url_for('correlacion_delete_project', nombre=p.nombre) }}" style="display:inline;" onsubmit="return confirm('Eliminar proyecto?');">
              <button class="btn gray" type="submit">Eliminar</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  {% if active_project %}
  <div class="card">
    <h3>Proyecto: {{ active_project.nombre }}</h3>
    <div style="margin-bottom:0.4rem; color:#555; font-size:0.9rem;">
      Rango: {{ active_project.fecha_ini }} a {{ active_project.fecha_fin }} | Tags: {{ active_project.tags|length }}
    </div>
    {% if active_project.tags and active_project.tags|length > 0 %}
    <div style="max-height:260px; overflow:auto; border:1px solid #ddd; padding:6px; background:#fff;">
      <table style="width:100%; border-collapse:collapse; font-size:0.9rem;">
        <thead><tr><th style="text-align:left;">codigo_tag</th><th>tagid</th><th>tipo de medidor</th></tr></thead>
        <tbody>
          {% for t in active_project.tags %}
          <tr>
            <td style="text-align:left; border-top:1px solid #eee;">{{ t.codigo_tag }}</td>
            <td style="border-top:1px solid #eee;">{{ t.tagid }}</td>
            <td style="border-top:1px solid #eee;">{{ t.medidor_tipo }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% else %}
    <div style="color:#9a3412; background:#fff7ed; border:1px solid #fed7aa; padding:0.6rem 0.8rem; border-radius:6px;">
      Este proyecto no tiene tags asociados aún. Usa “ingresar tags”.
    </div>
    {% endif %}
    <form method="post" action="{{ url_for('correlacion_build_project', nombre=active_project.nombre) }}" style="margin-top:0.6rem;">
      <button class="btn green" type="submit">Generar SQLite</button>
    </form>
    <div style="margin-top:0.6rem;">
      <a class="btn gray" href="{{ url_for('correlacion_results_index', project=active_project.nombre) }}">Ver resultados corr1</a>
    </div>
  </div>
  {% endif %}

  <script>
    function addRow() {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><input type="number" name="tagid[]" required></td>
        <td><input type="text" name="codigo_tag[]" readonly tabindex="-1" style="background:#f3f4f6;"></td>
        <td>
          <select name="medidor_tipo[]">
            <option value="caudal_l_s">caudal l/s</option>
            <option value="m3_hr">m3/hr</option>
            <option value="volumen_m3">volumen m3</option>
            <option value="cm">cm</option>
            <option value="presion">presion</option>
            <option value="otro">otro</option>
          </select>
        </td>
        <td><button type="button" class="btn gray" onclick="this.closest('tr').remove()">x</button></td>
      `;
      document.getElementById('tags-body').appendChild(tr);
      wireTagIdToCodigoForRow(tr);
    }
    async function fillCodigoTagForRow(row) {
      const tagInput = row.querySelector('input[name="tagid[]"]');
      const codigoInput = row.querySelector('input[name="codigo_tag[]"]');
      if (!tagInput || !codigoInput) return;
      const raw = (tagInput.value || '').trim();
      if (!raw) { codigoInput.value = ''; return; }
      try {
        const res = await fetch('/correlacion/api/taginfo?tagid=' + encodeURIComponent(raw));
        const data = await res.json();
        codigoInput.value = (data && data.codigo_tag) ? data.codigo_tag : '';
      } catch (e) {
        codigoInput.value = '';
      }
    }
    function wireTagIdToCodigoForRow(row) {
      const tagInput = row.querySelector('input[name="tagid[]"]');
      if (!tagInput) return;
      tagInput.addEventListener('input', () => fillCodigoTagForRow(row));
      fillCodigoTagForRow(row);
    }
    document.querySelectorAll('#tags-body tr').forEach(wireTagIdToCodigoForRow);
  </script>
</body>
</html>
"""


CORR_RESULTS_TEMPLATE = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>rt3-ia Resultados corr1</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 1.2rem; background: #f5f5f5; color: #222; }
    .topbar { display:flex; gap:0.5rem; flex-wrap:wrap; margin-bottom:1rem; }
    .btn { display:inline-block; padding:0.45rem 0.8rem; background:#1976d2; color:#fff; text-decoration:none; border:none; border-radius:4px; cursor:pointer; }
    .btn.gray { background:#757575; }
    .card { background:#fff; border:1px solid #d6d6d6; border-radius:8px; padding:1rem; margin-bottom:1rem; }
    .grid { display:grid; grid-template-columns: repeat(4, minmax(170px, 1fr)); gap:0.8rem; }
    .metric-title { color:#666; font-size:0.85rem; margin-bottom:0.35rem; }
    .metric-value { font-size:1.35rem; font-weight:700; }
    .hint { color:#555; font-size:0.9rem; }
    .row { display:grid; grid-template-columns: 180px 1fr; gap:0.7rem; align-items:center; margin-bottom:0.6rem; }
    input, select { padding:0.4rem 0.55rem; width:100%; box-sizing:border-box; }
    table { width:100%; border-collapse:collapse; background:#fff; }
    th, td { border:1px solid #ddd; padding:0.42rem 0.5rem; font-size:0.84rem; white-space:nowrap; text-align:left; }
    th { background:#eee; }
    .empty { background:#fff7ed; border:1px solid #fed7aa; color:#9a3412; padding:0.9rem 1rem; border-radius:8px; }
    code { background:#f3f4f6; padding:0.1rem 0.3rem; border-radius:4px; }
    .svg-wrap { width:100%; overflow-x:auto; border:1px solid #d6d6d6; border-radius:8px; background:#fff; }
    svg { width:100%; height:320px; display:block; }
    @media (max-width: 900px) {
      .grid { grid-template-columns: 1fr 1fr; }
      .row { grid-template-columns: 1fr; }
    }
    @media (max-width: 640px) {
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="topbar">
    <a href="{{ url_for('index') }}" class="btn gray">Volver a recintos</a>
    <a href="{{ url_for('correlacion_index', project=selected_project_name) if selected_project_name else url_for('correlacion_index') }}" class="btn gray">Volver a correlacion</a>
  </div>

  <h1>Resultados corr1</h1>

  <div class="card">
    <form method="get" action="{{ url_for('correlacion_results_index') }}">
      <div class="row">
        <label>Proyecto</label>
        <select name="project">
          <option value="">Seleccione proyecto</option>
          {% for p in projects %}
          <option value="{{ p.nombre }}" {% if selected_project_name == p.nombre %}selected{% endif %}>{{ p.nombre }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="row">
        <label>Directorio resultados</label>
        <input type="text" name="outdir" value="{{ outdir }}">
      </div>
      <div class="row">
        <label>Comparar con</label>
        <input type="text" name="outdir_compare" value="{{ outdir_compare or '' }}" placeholder="/home/criveras/app/rt3-ia/out_corr1_transformer">
      </div>
      <div class="row">
        <label>Series a superponer</label>
        <input type="text" name="series_tags" value="{{ series_tags }}" placeholder="cp_escorial_entrada_caudal,cp_rosario_consumoxhr,cp_nivel_tk_rosario2">
      </div>
      <div class="row">
        <label>Puntos historicos</label>
        <input type="number" name="series_limit" value="{{ series_limit }}">
      </div>
      <button type="submit" class="btn">Ver resultados</button>
    </form>
    <div class="hint" style="margin-top:0.8rem;">
      Si completas ambos directorios, Flask mostrará comparacion lado a lado de los dos modelos IA.
    </div>
  </div>

  {% if selected_project %}
  <div class="card">
    <div><b>Proyecto:</b> {{ selected_project.nombre }}</div>
    <div class="hint" style="margin-top:0.3rem;"><b>SQLite:</b> {{ selected_project.sqlite_file }}</div>
    <div class="hint" style="margin-top:0.3rem;"><b>Rango:</b> {{ selected_project.fecha_ini }} a {{ selected_project.fecha_fin }}</div>
    <div class="hint" style="margin-top:0.3rem;"><b>Outdir actual:</b> {{ outdir }}</div>
  </div>
  {% endif %}

  {% if result.errors %}
  <div class="card" style="border-color:#ffcc80; background:#fff8e1;">
    {% for err in result.errors %}
    <div>{{ err }}</div>
    {% endfor %}
  </div>
  {% endif %}

  {% if compare %}
  <div class="card">
    <h3>Comparacion de modelos IA</h3>
    {% for line in compare.winner_summary %}
    <div class="hint" style="margin-bottom:0.3rem;">{{ line }}</div>
    {% endfor %}
    <table>
      <thead><tr><th>Metrica</th><th>{{ compare.label_a }}</th><th>{{ compare.label_b }}</th><th>Mejor</th></tr></thead>
      <tbody>
        {% for row in compare.metric_rows %}
        <tr>
          <td>{{ row.name }}</td>
          <td>{% if row.a is not none %}{{ "%.6f"|format(row.a) }}{% else %}-{% endif %}</td>
          <td>{% if row.b is not none %}{{ "%.6f"|format(row.b) }}{% else %}-{% endif %}</td>
          <td>{{ row.winner }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  {% if compare.forecast_rows %}
  <div class="card">
    <h3>Forecast A vs B</h3>
    <div class="hint" style="margin-bottom:0.7rem;">
      Delta = B - A. Se muestran las primeras 24 filas compartidas por fecha.
    </div>
    <table>
      <thead><tr><th>Fecha</th><th>{{ compare.label_a }}</th><th>{{ compare.label_b }}</th><th>Delta</th></tr></thead>
      <tbody>
        {% for row in compare.forecast_rows %}
        <tr>
          <td>{{ row.fecha }}</td>
          <td>{{ row.a }}</td>
          <td>{{ row.b }}</td>
          <td>{% if row.delta is not none %}{{ "%.6f"|format(row.delta) }}{% else %}-{% endif %}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}
  {% endif %}

  {% if result.insights %}
  <div class="card">
    <h3>Hallazgos utiles</h3>
    {% for line in result.insights %}
    <div class="hint" style="margin-bottom:0.45rem;">{{ line }}</div>
    {% endfor %}
  </div>
  {% endif %}

  {% if series_analysis.datasets %}
  <div class="card">
    <h3>Series temporales superpuestas</h3>
    <div class="hint" style="margin-bottom:0.45rem;">
      Las series se muestran normalizadas para poder compararlas entre sí aunque tengan escalas distintas.
    </div>
    {% for line in series_analysis.insights %}
    <div class="hint" style="margin-bottom:0.35rem;">{{ line }}</div>
    {% endfor %}
    <div class="hint" style="margin-bottom:0.7rem;">
      Circulos = anomalias puntuales. Rectangulos = ventanas donde las dos primeras series tienen correlacion fuerte.
    </div>
    <div class="svg-wrap"><svg id="chart-series-overlay" viewBox="0 0 980 320" preserveAspectRatio="none"></svg></div>
  </div>
  {% endif %}

  {% if result.metrics %}
  <div class="grid" style="margin-bottom:1rem;">
    <div class="card">
      <div class="metric-title">Target</div>
      <div class="metric-value">{{ result.metrics.target or "-" }}</div>
    </div>
    <div class="card">
      <div class="metric-title">Modelo</div>
      <div class="metric-value">{{ result.metrics.model or "-" }}</div>
    </div>
    <div class="card">
      <div class="metric-title">Ventana / horizonte</div>
      <div class="metric-value">{{ result.metrics.window or "-" }} / {{ result.metrics.horizon or "-" }}</div>
    </div>
    <div class="card">
      <div class="metric-title">Test MAE</div>
      <div class="metric-value">
        {% if result.metrics.metrics and result.metrics.metrics.test_mae is not none %}
        {{ "%.6f"|format(result.metrics.metrics.test_mae) }}
        {% else %}-{% endif %}
      </div>
    </div>
  </div>

  <div class="grid" style="margin-bottom:1rem;">
    <div class="card">
      <div class="metric-title">Train MSE</div>
      <div class="metric-value">{% if result.metrics.metrics and result.metrics.metrics.train_mse is not none %}{{ "%.6f"|format(result.metrics.metrics.train_mse) }}{% else %}-{% endif %}</div>
    </div>
    <div class="card">
      <div class="metric-title">Val MSE</div>
      <div class="metric-value">{% if result.metrics.metrics and result.metrics.metrics.val_mse is not none %}{{ "%.6f"|format(result.metrics.metrics.val_mse) }}{% else %}-{% endif %}</div>
    </div>
    <div class="card">
      <div class="metric-title">Test MSE</div>
      <div class="metric-value">{% if result.metrics.metrics and result.metrics.metrics.test_mse is not none %}{{ "%.6f"|format(result.metrics.metrics.test_mse) }}{% else %}-{% endif %}</div>
    </div>
    <div class="card">
      <div class="metric-title">Test MAE</div>
      <div class="metric-value">{% if result.metrics.metrics and result.metrics.metrics.test_mae is not none %}{{ "%.6f"|format(result.metrics.metrics.test_mae) }}{% else %}-{% endif %}</div>
    </div>
  </div>
  {% endif %}

  {% if result.top_corr_rows %}
  <div class="card">
    <h3>Correlaciones más fuertes con el target</h3>
    <div class="hint" style="margin-bottom:0.7rem;">
      Esta tabla muestra asociación lineal. Sirve para priorizar variables relacionadas, pero no prueba causalidad.
    </div>
    <table>
      <thead><tr><th>Tag</th><th>Correlación</th></tr></thead>
      <tbody>
        {% for row in result.top_corr_rows %}
        <tr>
          <td>{{ row.tag }}</td>
          <td>{% if row.corr is not none %}{{ "%.6f"|format(row.corr) }}{% endif %}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}

  {% if result.focus_driver_rows %}
  <div class="card">
    <h3>Grafico de impacto de {{ result.focus_driver }}</h3>
    <div class="hint" style="margin-bottom:0.7rem;">
      Barras hacia la derecha: tienden a subir juntas. Barras hacia la izquierda: una sube y la otra tiende a bajar.
    </div>
    <div class="svg-wrap"><svg id="chart-driver-impact" viewBox="0 0 980 320" preserveAspectRatio="none"></svg></div>
  </div>

  <div class="card">
    <h3>Si aumenta {{ result.focus_driver }}, ¿qué tags sentirían más efecto?</h3>
    <div class="hint" style="margin-bottom:0.7rem;">
      Esto es una lectura exploratoria basada en correlación. No es causalidad, pero sí una buena priorización inicial.
    </div>
    <table>
      <thead><tr><th>Tag</th><th>Tendencia esperada</th><th>Nivel de impacto</th><th>Lectura rapida</th><th>Correlación</th></tr></thead>
      <tbody>
        {% for row in result.focus_driver_impact_rows %}
        <tr>
          <td>{{ row.tag }}</td>
          <td>{{ row.direction }}</td>
          <td>
            {% if row.impact_level == 'alto' %}
            <span style="background:#c8e6c9; padding:0.12rem 0.35rem; border-radius:4px;">alto</span>
            {% elif row.impact_level == 'medio' %}
            <span style="background:#fff9c4; padding:0.12rem 0.35rem; border-radius:4px;">medio</span>
            {% elif row.impact_level == 'bajo' %}
            <span style="background:#ffe0b2; padding:0.12rem 0.35rem; border-radius:4px;">bajo</span>
            {% else %}
            <span style="background:#eceff1; padding:0.12rem 0.35rem; border-radius:4px;">muy bajo</span>
            {% endif %}
          </td>
          <td>{{ row.impact_hint }}</td>
          <td>{{ "%.6f"|format(row.corr) }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h3>Relación de {{ result.focus_driver }} con el resto</h3>
    <div class="hint" style="margin-bottom:0.7rem;">
      Si quieres estudiar qué tags se mueven junto con un aumento de entrada de Escorial, esta fila es el primer mapa rápido.
    </div>
    <table>
      <thead><tr><th>Tag</th><th>Correlación con {{ result.focus_driver }}</th></tr></thead>
      <tbody>
        {% for row in result.focus_driver_rows %}
        <tr>
          <td>{{ row.tag }}</td>
          <td>{% if row.corr is not none %}{{ "%.6f"|format(row.corr) }}{% endif %}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}

  {% if result.matrix_rows and result.matrix_headers %}
  <div class="card">
    <h3>Matriz X/Y completa de correlación</h3>
    <div class="hint" style="margin-bottom:0.7rem;">
      Eje Y = variable fila. Eje X = variable columna. Valores cercanos a 1 indican que ambas series suben y bajan juntas. Valores cercanos a -1 indican comportamiento inverso.
    </div>
    <div style="overflow:auto;">
      <table>
        <thead>
          <tr>
            <th>Tag</th>
            {% for header in result.matrix_headers %}
            <th>{{ header }}</th>
            {% endfor %}
          </tr>
        </thead>
        <tbody>
          {% for row in result.matrix_rows %}
          <tr {% if row.tag == result.focus_driver %}style="background:#e3f2fd;"{% endif %}>
            <td><b>{{ row.tag }}</b></td>
            {% for item in row["values"] %}
            <td {% if item.corr is not none and item.corr|abs >= 0.8 %}style="background:#e8f5e9;"{% elif item.corr is not none and item.corr|abs >= 0.5 %}style="background:#fff8e1;"{% endif %}>
              {% if item.corr is not none %}{{ "%.4f"|format(item.corr) }}{% endif %}
            </td>
            {% endfor %}
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% endif %}

  {% if result.forecast_rows %}
  <div class="card">
    <h3>Forecast generado</h3>
    <table>
      <thead>
        <tr>
          {% for key in result.forecast_rows[0].keys() %}
          <th>{{ key }}</th>
          {% endfor %}
        </tr>
      </thead>
      <tbody>
        {% for row in result.forecast_rows %}
        <tr>
          {% for value in row.values() %}
          <td>{{ value }}</td>
          {% endfor %}
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}

  {% if not result.metrics and not result.forecast_rows %}
  <div class="empty">
    Todavía no hay resultados visibles para este directorio. Primero ejecuta <code>corr1.py</code> y apunta esta vista al mismo <code>--outdir</code>.
  </div>
  {% endif %}
  <script>
    function renderOverlayChart(svgId, labels, datasets, markers, corrWindows) {
      const svg = document.getElementById(svgId);
      if (!svg || !labels || !labels.length || !datasets || !datasets.length) return;
      const width = 980;
      const height = 320;
      const pad = { left: 56, right: 18, top: 18, bottom: 38 };
      const plotW = width - pad.left - pad.right;
      const plotH = height - pad.top - pad.bottom;
      const xFor = i => pad.left + (labels.length <= 1 ? 0 : (i / (labels.length - 1)) * plotW);
      const yFor = v => pad.top + ((3 - v) / 6) * plotH;
      let html = '';
      for (let i = 0; i < 7; i++) {
        const val = 3 - i;
        const y = yFor(val);
        html += '<line x1="' + pad.left + '" y1="' + y + '" x2="' + (width - pad.right) + '" y2="' + y + '" stroke="#eef2f7" stroke-width="1" />';
        html += '<text x="8" y="' + (y + 4) + '" fill="#6b7280" font-size="11">' + val.toFixed(0) + '</text>';
      }
      (corrWindows || []).forEach(win => {
        const x1 = xFor(win.start);
        const x2 = xFor(win.end);
        const fill = win.corr >= 0 ? 'rgba(46, 125, 50, 0.12)' : 'rgba(198, 40, 40, 0.12)';
        html += '<rect x="' + x1 + '" y="' + pad.top + '" width="' + Math.max(2, x2 - x1) + '" height="' + plotH + '" fill="' + fill + '" stroke="none" />';
      });
      datasets.forEach(ds => {
        let path = '';
        let started = false;
        (ds.values || []).forEach((raw, i) => {
          if (raw === null || raw === undefined || Number.isNaN(raw)) {
            started = false;
            return;
          }
          const x = xFor(i);
          const y = yFor(Number(raw));
          path += (started ? ' L ' : ' M ') + x + ' ' + y;
          started = true;
        });
        if (path) {
          html += '<path d="' + path + '" fill="none" stroke="' + ds.color + '" stroke-width="2.1" />';
        }
      });
      (markers || []).forEach(marker => {
        const ds = datasets.find(item => item.name === marker.series);
        if (!ds) return;
        const value = ds.values[marker.index];
        if (value === null || value === undefined || Number.isNaN(value)) return;
        const x = xFor(marker.index);
        const y = yFor(Number(value));
        html += '<circle cx="' + x + '" cy="' + y + '" r="5" fill="#fff" stroke="' + ds.color + '" stroke-width="2.2" />';
      });
      const tickStep = Math.max(1, Math.floor(labels.length / 8));
      for (let i = 0; i < labels.length; i += tickStep) {
        const x = xFor(i);
        html += '<text x="' + x + '" y="' + (height - 12) + '" fill="#6b7280" font-size="11" text-anchor="middle">' + labels[i].slice(5, 16).replace("T", " ") + '</text>';
      }
      html += '<rect x="' + pad.left + '" y="' + pad.top + '" width="' + plotW + '" height="' + plotH + '" fill="none" stroke="#cbd5e1" stroke-width="1" />';
      let legendY = 14;
      datasets.forEach(ds => {
        html += '<circle cx="' + (pad.left + 8) + '" cy="' + legendY + '" r="4" fill="' + ds.color + '" />';
        html += '<text x="' + (pad.left + 18) + '" y="' + (legendY + 4) + '" fill="#374151" font-size="12">' + ds.name + '</text>';
        legendY += 14;
      });
      svg.innerHTML = html;
    }

    function renderImpactChart(svgId, rows) {
      const svg = document.getElementById(svgId);
      if (!svg || !rows || !rows.length) return;
      const width = 980;
      const height = Math.max(320, rows.length * 28 + 40);
      svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
      const pad = { left: 240, right: 40, top: 20, bottom: 20 };
      const plotW = width - pad.left - pad.right;
      const centerX = pad.left + plotW / 2;
      let html = '';
      html += '<line x1="' + centerX + '" y1="' + pad.top + '" x2="' + centerX + '" y2="' + (height - pad.bottom) + '" stroke="#9ca3af" stroke-width="1.5" />';
      html += '<text x="' + (centerX - 110) + '" y="14" fill="#6b7280" font-size="12">baja</text>';
      html += '<text x="' + (centerX + 90) + '" y="14" fill="#6b7280" font-size="12">sube</text>';
      rows.forEach((row, idx) => {
        const corr = Number(row.corr || 0);
        const y = pad.top + idx * 28 + 8;
        const half = plotW / 2;
        const barW = Math.max(1, Math.abs(corr) * half);
        const x = corr >= 0 ? centerX : centerX - barW;
        const color = corr >= 0 ? '#2e7d32' : '#c62828';
        html += '<text x="' + (pad.left - 10) + '" y="' + (y + 11) + '" text-anchor="end" fill="#374151" font-size="12">' + row.tag + '</text>';
        html += '<rect x="' + x + '" y="' + y + '" width="' + barW + '" height="16" rx="3" fill="' + color + '" opacity="0.85" />';
        html += '<text x="' + (corr >= 0 ? x + barW + 6 : x - 6) + '" y="' + (y + 12) + '" text-anchor="' + (corr >= 0 ? 'start' : 'end') + '" fill="#374151" font-size="11">' + corr.toFixed(3) + '</text>';
      });
      svg.innerHTML = html;
    }
    renderOverlayChart(
      'chart-series-overlay',
      {{ series_analysis.labels|tojson }},
      {{ series_analysis.datasets|tojson }},
      {{ series_analysis.markers|tojson }},
      {{ series_analysis.corr_windows|tojson }}
    );
    renderImpactChart('chart-driver-impact', {{ result.focus_driver_impact_rows[:8]|tojson }});
  </script>
</body>
</html>
"""


DETAIL_TEMPLATE = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Detalle recinto {{ nombre }}</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 2rem; background: #f5f5f5; color: #222; }
    h1 { color: #333; }
    .table-wrap { background:#fff; border:1px solid #d6d6d6; border-radius:8px; overflow:auto; margin-top:1rem; }
    table { width:100%; min-width: 520px; background:#fff; border-collapse:collapse; }
    th, td { border:1px solid #ccc; padding:0.35rem 0.5rem; font-size:0.8rem; white-space:nowrap; }
    th { background:#e0e0e0; position: sticky; top: 0; }
    td:first-child { font-family: monospace; font-size: 0.76rem; }
    .btn { display:inline-block; padding:0.4rem 0.8rem; background:#1976d2; color:#fff; text-decoration:none; border-radius:4px; }
    .btn:hover { background:#145ea8; }
    .field { width: 100%; padding:0.4rem 0.6rem; margin-top:0.35rem; box-sizing:border-box; border:1px solid #bdbdbd; border-radius:4px; background:#fff; }
    .card { background:#fff; border:1px solid #d6d6d6; border-radius:8px; padding:0.8rem; margin-top:1rem; }
    .btn2 { display:inline-block; padding:0.35rem 0.7rem; background:#388e3c; color:#fff; border-radius:4px; border:none; cursor:pointer; }
    .btn2:hover { background:#2e7d32; }
  </style>
</head>
<body>
  <a href="{{ url_for('index') }}" class="btn">Volver</a>
  <a href="{{ url_for('ver_recinto_ia', nombre=nombre) }}" class="btn" style="margin-left:0.35rem;">IA / proyección</a>
  <h1>Recinto {{ nombre }}</h1>

  <form method="post" action="{{ url_for('renombrar_recinto', nombre=nombre) }}" class="card">
    <label>Editar nombre del recinto</label>
    <input class="field" type="text" name="nuevo_nombre" value="{{ nombre }}" required>
    <div style="margin-top:0.6rem;">
      <button type="submit" class="btn2">Guardar nombre</button>
    </div>
  </form>

  <h2>Datos ({{ filas|length }} registros)</h2>
  <div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>Fecha</th>
        <th>qin</th>
        <th>vol</th>
        <th>qout (l/s)</th>
      </tr>
    </thead>
    <tbody>
      {% for f in filas %}
      <tr>
        <td>{{ f.fecha }}</td>
        <td>{{ "%.3f"|format(f.qin) if f.qin is not none else "" }}</td>
        <td>{{ "%.3f"|format(f.vol) if f.vol is not none else "" }}</td>
        <td>{{ "%.3f"|format(f.qout) if f.qout is not none else "" }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  </div>
</body>
</html>
"""


IA_TEMPLATE = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>IA Consumo {{ nombre }}</title>
  <style>
    :root {
      --bg: #f3f4f6;
      --card: #ffffff;
      --line: #d5d7db;
      --text: #1f2937;
      --muted: #6b7280;
      --blue: #0f6cbd;
      --green: #2e7d32;
      --orange: #ef6c00;
      --red: #c62828;
    }
    body { font-family: Arial, sans-serif; margin: 1.2rem; background: var(--bg); color: var(--text); }
    h1, h2, h3 { margin: 0 0 0.8rem; }
    .topbar { display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap; margin-bottom: 1rem; }
    .btn { display:inline-block; padding:0.5rem 0.9rem; background:var(--blue); color:#fff; text-decoration:none; border-radius:6px; border:none; cursor:pointer; }
    .btn:hover { filter: brightness(0.95); }
    .btn-muted { background:#6b7280; }
    .btn-green { background:var(--green); }
    .grid { display:grid; grid-template-columns: repeat(4, minmax(160px, 1fr)); gap: 0.8rem; margin-bottom: 1rem; }
    .card { background: var(--card); border:1px solid var(--line); border-radius: 10px; padding: 1rem; box-shadow: 0 1px 4px rgba(0,0,0,0.04); }
    .metric-title { color: var(--muted); font-size: 0.88rem; margin-bottom: 0.35rem; }
    .metric-value { font-size: 1.45rem; font-weight: 700; }
    .metric-sub { color: var(--muted); font-size: 0.82rem; margin-top:0.2rem; }
    .row-2 { display:grid; grid-template-columns: 1.2fr 1fr; gap: 1rem; margin-bottom:1rem; }
    .form-grid { display:grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap:0.8rem; align-items:end; }
    label { display:block; font-weight:700; margin-bottom:0.35rem; }
    input[type=number], select { width:100%; padding:0.55rem 0.65rem; box-sizing:border-box; border:1px solid #bdbdbd; border-radius:6px; background:#fff; }
    .hint { color:var(--muted); font-size:0.85rem; margin-top:0.45rem; }
    .chart-title { display:flex; justify-content:space-between; gap:0.5rem; align-items:center; margin-bottom:0.7rem; }
    .legend { display:flex; gap:0.85rem; flex-wrap:wrap; color:var(--muted); font-size:0.85rem; }
    .legend span::before { content:""; display:inline-block; width:10px; height:10px; border-radius:999px; margin-right:0.35rem; vertical-align:middle; }
    .legend .avg::before { background:#2563eb; }
    .legend .ia::before { background:#ef6c00; }
    .legend .weekday::before { background:#ef6c00; }
    .legend .weekend::before { background:#2563eb; }
    .legend .qin::before { background:#2e7d32; }
    .legend .ideal::before { background:#c62828; }
    .legend .band::before { background:#c8e6c9; border:1px solid #66bb6a; }
    .legend .idealvol::before { background:#6b7280; }
    .legend .real::before { background:#00838f; }
    .segmented { display:flex; gap:0.45rem; align-items:center; flex-wrap:wrap; }
    .segmented .btn { padding:0.35rem 0.7rem; }
    .svg-wrap { width:100%; overflow-x:auto; border:1px solid var(--line); border-radius:8px; background:#fff; }
    .chart-shell { position: relative; }
    .chart-tooltip {
      position: absolute;
      display: none;
      pointer-events: none;
      background: rgba(17, 24, 39, 0.94);
      color: #fff;
      padding: 0.45rem 0.6rem;
      border-radius: 6px;
      font-size: 0.78rem;
      line-height: 1.35;
      box-shadow: 0 4px 16px rgba(0,0,0,0.18);
      white-space: nowrap;
      z-index: 5;
    }
    svg { width:100%; height:320px; display:block; }
    .table-wrap { overflow:auto; border:1px solid var(--line); border-radius:8px; background:#fff; }
    table { width:100%; border-collapse:collapse; min-width:720px; }
    th, td { border:1px solid #e5e7eb; padding:0.42rem 0.5rem; white-space:nowrap; font-size:0.82rem; text-align:right; }
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align:left; }
    th { background:#f8fafc; position:sticky; top:0; }
    .empty { background:#fff7ed; border:1px solid #fed7aa; color:#9a3412; padding:0.9rem 1rem; border-radius:8px; }
    @media (max-width: 1080px) {
      .grid { grid-template-columns: repeat(2, minmax(160px, 1fr)); }
      .row-2 { grid-template-columns: 1fr; }
    }
    @media (max-width: 720px) {
      body { margin: 0.8rem; }
      .grid { grid-template-columns: 1fr; }
      .form-grid { grid-template-columns: 1fr; }
      svg { height:260px; }
    }
  </style>
</head>
<body>
  <div class="topbar">
    <a href="{{ url_for('index') }}" class="btn btn-muted">Volver</a>
    <a href="{{ url_for('ver_recinto', nombre=nombre) }}" class="btn btn-muted">Tabla</a>
  </div>

  <h1>IA consumo y proyección: {{ nombre }}</h1>
  <div class="hint" style="margin-bottom:0.9rem;">Perfil activo de proyección: {{ perfil_tipo_label }}</div>

  {% if not perfil_disponible %}
    <div class="empty">
      No existe la tabla <code>perfil_consumo_15min</code> para este recinto. Ejecuta <code>demo_ia2.py --sqlite {{ sqlite_path }}</code> y vuelve a entrar.
    </div>
  {% else %}
    <div class="grid">
      <div class="card">
        <div class="metric-title">Último volumen</div>
        <div class="metric-value">{{ "%.2f"|format(last_vol) if last_vol is not none else "-" }}</div>
        <div class="metric-sub">m3 en {{ last_fecha or "-" }}</div>
      </div>
      <div class="card">
        <div class="metric-title">Último qin</div>
        <div class="metric-value">{{ "%.2f"|format(last_qin) if last_qin is not none else "-" }}</div>
        <div class="metric-sub">
          l/s
          {% if last_qin is not none %}
          | {{ "%.2f"|format(last_qin * 3.6) }} m3/hr
          {% endif %}
          {% if last_qin_fecha %}
          | dato qin: {{ last_qin_fecha }}
          {% endif %}
        </div>
      </div>
      <div class="card">
        <div class="metric-title">Maximo qin historico</div>
        <div class="metric-value">{{ "%.2f"|format(max_qin_historico) if max_qin_historico is not none else "-" }}</div>
        <div class="metric-sub">
          l/s en SQLite historico
          {% if max_qin_historico is not none %}
          | {{ "%.2f"|format(max_qin_historico * 3.6) }} m3/hr
          {% endif %}
        </div>
      </div>
      <div class="card">
        <div class="metric-title">Umbral alto ideal</div>
        <div class="metric-value">{{ "%.2f"|format(target_vol) if target_vol is not none else "-" }}</div>
        <div class="metric-sub">90% del volumen máximo, ajustado con consumo IA</div>
      </div>
      <div class="card">
        <div class="metric-title">Qin ideal sugerido</div>
        <div class="metric-value">{{ "%.2f"|format(qin_requerido) if qin_requerido is not none else "-" }}</div>
        <div class="metric-sub">l/s para que la proyeccion alcance el umbral maximo ideal usando consumo IA, sin pasarse del limite ideal</div>
      </div>
      <div class="card">
        <div class="metric-title">{{ rebalse_card.title }}</div>
        <div class="metric-value">{{ rebalse_card.value }}</div>
        <div class="metric-sub">{{ rebalse_card.sub }}</div>
      </div>
      <div class="card">
        <div class="metric-title">{{ bajo_10_card.title }}</div>
        <div class="metric-value">{{ bajo_10_card.value }}</div>
        <div class="metric-sub">{{ bajo_10_card.sub }}</div>
      </div>
    </div>

    {% if volumen_banda_min is not none and volumen_banda_max is not none %}
    <div class="card" style="margin-bottom:1rem;">
      <div class="metric-title">Banda ideal de operación del estanque</div>
      <div class="metric-value">{{ "%.2f"|format(volumen_banda_min) }} a {{ "%.2f"|format(volumen_banda_max) }} m3</div>
      <div class="metric-sub">Calculada como 50% a 90% del volumen máximo configurado del recinto</div>
    </div>
    {% endif %}

    <div class="row-2">
      <div class="card">
        <h2>Simular con caudal fijo de entrada</h2>
        <form method="get" action="{{ url_for('ver_recinto_ia', nombre=nombre) }}">
          <div class="form-grid">
            <div>
              <label>Qin fijo (l/s)</label>
              <input type="number" step="0.01" name="qin_fijo" value="{{ qin_fijo }}">
              {% if qin_requerido is not none %}
              <div class="hint">Sugerencia IA: {{ "%.2f"|format(qin_requerido) }} l/s</div>
              {% endif %}
            </div>
            <div>
              <label>Objetivo opcional (m3)</label>
              <input type="number" step="0.01" name="target_vol" value="{{ target_vol_input }}">
            </div>
            <div>
              <label>Perfil de consumo IA</label>
              <select name="profile_group">
                <option value="weekday" {% if selected_profile_group == 'weekday' %}selected{% endif %}>Lunes a viernes</option>
                <option value="weekend" {% if selected_profile_group == 'weekend' %}selected{% endif %}>Fin de semana</option>
              </select>
              <div class="hint">Por defecto se usa {{ default_profile_group_label }} según el día actual.</div>
            </div>
            <div>
              <button type="submit" class="btn btn-green">Simular proyección</button>
              {% if qin_requerido is not none %}
              <div style="margin-top:0.45rem;">
                <a class="btn btn-muted" href="{{ url_for('ver_recinto_ia', nombre=nombre, qin_fijo=('%.2f'|format(qin_requerido)), target_vol=target_vol_input, vol_view=vol_view, profile_group=selected_profile_group) }}">Usar qin ideal</a>
              </div>
              {% endif %}
            </div>
          </div>
        </form>
        <div class="hint">
          La simulación usa bloques de 15 minutos durante las próximas 24 horas y proyecta el consumo IA según el perfil seleccionado.
        </div>
      </div>
      <div class="card">
        <h2>Resumen 24h</h2>
        <div class="metric-title">Consumo promedio esperado</div>
        <div class="metric-value">{{ "%.2f"|format(qout_avg_24h) if qout_avg_24h is not none else "-" }}</div>
        <div class="metric-sub">l/s promedio de los 96 slots</div>
        <div style="height:0.8rem;"></div>
        <div class="metric-title">Consumo IA esperado</div>
        <div class="metric-value">{{ "%.2f"|format(qout_ia_24h) if qout_ia_24h is not none else "-" }}</div>
        <div class="metric-sub">l/s promedio de los 96 slots</div>
        {% if meta %}
        <div class="hint" style="margin-top:0.9rem;">
          Modelo: {{ meta.get("model_kind", "-") }} |
          entrenamiento: {{ meta.get("trained_at", "-") }} |
          filas train: {{ meta.get("training_rows", "-") }}
        </div>
        {% endif %}
      </div>
    </div>

    <div class="card">
      <div class="chart-title">
        <h2>Volumen proyectado hoy 00:00 a mañana 12:00 con qin fijo = {{ "%.2f"|format(qin_fijo) }} l/s</h2>
        <div class="segmented">
          <a class="btn {% if vol_view == 'today' %}btn-green{% else %}btn-muted{% endif %}" href="{{ url_for('ver_recinto_ia', nombre=nombre, qin_fijo=qin_fijo, target_vol=target_vol_input, vol_view='today', profile_group=selected_profile_group) }}">Hoy</a>
          <a class="btn {% if vol_view == 'prev7' %}btn-green{% else %}btn-muted{% endif %}" href="{{ url_for('ver_recinto_ia', nombre=nombre, qin_fijo=qin_fijo, target_vol=target_vol_input, vol_view='prev7', profile_group=selected_profile_group) }}">7 dias anterior</a>
        </div>
      </div>
      <div class="chart-title" style="margin-top:-0.2rem;">
        <div class="legend">
          <span class="ia">Volumen proyectado IA</span>
          <span class="idealvol">Volumen ideal IA</span>
          <span class="qin">{{ 'Volumen real 7 dias' if vol_view == 'prev7' else 'Volumen real hoy' }}</span>
          <span class="band">Banda ideal 50%-90%</span>
          <span class="ideal">Umbral alto ideal</span>
        </div>
      </div>
      <div class="svg-wrap chart-shell">
        <svg id="chart-vol" viewBox="0 0 980 320" preserveAspectRatio="none"></svg>
        <div id="chart-vol-tooltip" class="chart-tooltip"></div>
      </div>
    </div>

    <div class="card">
      <div class="chart-title">
        <h2>Consumo ideal diario IA 00:00 a 23:45</h2>
        <div class="legend">
          <span class="weekday">IA lunes a viernes</span>
          <span class="weekend">IA fin de semana</span>
          <span class="real">Consumo real dia anterior</span>
        </div>
      </div>
      <div class="svg-wrap"><svg id="chart-qout" viewBox="0 0 980 320" preserveAspectRatio="none"></svg></div>
    </div>

    <div class="card">
      <div class="chart-title">
        <h2>Tendencia reciente y referencia</h2>
        <div class="legend">
          <span class="qin">Qin reciente</span>
          <span class="avg">Qout reciente</span>
        </div>
      </div>
      <div class="svg-wrap"><svg id="chart-hist" viewBox="0 0 980 320" preserveAspectRatio="none"></svg></div>
    </div>

    <div class="card">
      <h2>Tabla de proyección 24h</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Fecha</th>
              <th>Hora</th>
              <th>Qout IA (m3/hr)</th>
              <th>Qin fijo (m3/hr)</th>
              <th>Delta Qin-Qout (m3/hr)</th>
              <th>Vol IA con qin fijo</th>
            </tr>
          </thead>
          <tbody>
            {% for row in projection_table %}
            <tr>
              <td>{{ row.timestamp[:10] }}</td>
              <td>{{ row.hora_texto }}</td>
              <td>{{ "%.3f"|format(row.qout_ia * 3.6) if row.qout_ia is not none else "" }}</td>
              <td>{{ "%.3f"|format(qin_fijo * 3.6) }}</td>
              <td>{{ "%.3f"|format((qin_fijo - row.qout_ia) * 3.6) if row.qout_ia is not none else "" }}</td>
              <td>{{ "%.3f"|format(row.vol_ia) if row.vol_ia is not none else "" }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <script>
      function renderLineChart(svgId, labels, datasets, options) {
        const svg = document.getElementById(svgId);
        if (!svg || !labels || labels.length === 0) return;
        const tooltip = document.getElementById(svgId + '-tooltip');
        const width = 980;
        const height = 320;
        const pad = { left: 56, right: 18, top: 18, bottom: 38 };
        const plotW = width - pad.left - pad.right;
        const plotH = height - pad.top - pad.bottom;
        const allValues = [];
        datasets.forEach(ds => (ds.values || []).forEach(v => {
          if (v !== null && v !== undefined && !Number.isNaN(v)) allValues.push(Number(v));
        }));
        if (options && options.horizontal !== undefined && options.horizontal !== null) {
          allValues.push(Number(options.horizontal));
        }
        if (options && options.bandMin !== undefined && options.bandMin !== null) {
          allValues.push(Number(options.bandMin));
        }
        if (options && options.bandMax !== undefined && options.bandMax !== null) {
          allValues.push(Number(options.bandMax));
        }
        if (!allValues.length) {
          svg.innerHTML = '<text x="20" y="40" fill="#6b7280">Sin datos para graficar</text>';
          return;
        }
        let minY = options && options.minY !== undefined && options.minY !== null
          ? Number(options.minY)
          : Math.min.apply(null, allValues);
        let maxY = options && options.maxY !== undefined && options.maxY !== null
          ? Number(options.maxY)
          : Math.max.apply(null, allValues);
        if (minY === maxY) {
          minY -= 1;
          maxY += 1;
        }
        if (!(options && options.minY !== undefined && options.minY !== null && options.maxY !== undefined && options.maxY !== null)) {
          const paddingY = (maxY - minY) * 0.08;
          minY -= paddingY;
          maxY += paddingY;
        }
        const xFor = i => pad.left + (labels.length <= 1 ? 0 : (i / (labels.length - 1)) * plotW);
        const xForFraction = i => pad.left + (labels.length <= 1 ? 0 : (i / (labels.length - 1)) * plotW);
        const yFor = v => pad.top + ((maxY - v) / (maxY - minY)) * plotH;
        let html = '';
        if (options && options.bandMin !== undefined && options.bandMin !== null && options.bandMax !== undefined && options.bandMax !== null) {
          const yTop = yFor(Number(options.bandMax));
          const yBottom = yFor(Number(options.bandMin));
          html += '<rect x="' + pad.left + '" y="' + yTop + '" width="' + plotW + '" height="' + Math.max(0, yBottom - yTop) + '" fill="rgba(102, 187, 106, 0.18)" stroke="rgba(102, 187, 106, 0.45)" stroke-width="1" />';
        }
        for (let i = 0; i < 5; i++) {
          const y = pad.top + (plotH / 4) * i;
          const value = maxY - ((maxY - minY) / 4) * i;
          html += '<line x1="' + pad.left + '" y1="' + y + '" x2="' + (width - pad.right) + '" y2="' + y + '" stroke="#e5e7eb" stroke-width="1" />';
          html += '<text x="10" y="' + (y + 4) + '" fill="#6b7280" font-size="11">' + value.toFixed(1) + '</text>';
        }
        const tickStep = Math.max(1, Math.floor(labels.length / 8));
        for (let i = 0; i < labels.length; i += tickStep) {
          const x = xFor(i);
          html += '<line x1="' + x + '" y1="' + pad.top + '" x2="' + x + '" y2="' + (height - pad.bottom) + '" stroke="#f1f5f9" stroke-width="1" />';
          html += '<text x="' + x + '" y="' + (height - 12) + '" fill="#6b7280" font-size="11" text-anchor="middle">' + labels[i] + '</text>';
        }
        datasets.forEach(ds => {
          let path = '';
          let started = false;
          (ds.values || []).forEach((raw, i) => {
            if (raw === null || raw === undefined || Number.isNaN(raw)) {
              started = false;
              return;
            }
            const x = xFor(i);
            const y = yFor(Number(raw));
            path += (started ? ' L ' : ' M ') + x + ' ' + y;
            started = true;
          });
          if (path) {
            html += '<path d="' + path + '" fill="none" stroke="' + ds.color + '" stroke-width="2.3" />';
          }
        });
        if (options && options.horizontal !== undefined && options.horizontal !== null) {
          const y = yFor(Number(options.horizontal));
          html += '<line x1="' + pad.left + '" y1="' + y + '" x2="' + (width - pad.right) + '" y2="' + y + '" stroke="#c62828" stroke-dasharray="6 4" stroke-width="2" />';
        }
        if (options && Array.isArray(options.verticalLines)) {
          options.verticalLines.forEach(line => {
            if (!line || line.index === undefined || line.index === null) return;
            const x = xForFraction(Number(line.index));
            const color = line.color || '#c62828';
            const dash = line.dash || '';
            html += '<line x1="' + x + '" y1="' + pad.top + '" x2="' + x + '" y2="' + (height - pad.bottom) + '" stroke="' + color + '" ' + (dash ? 'stroke-dasharray="' + dash + '"' : '') + ' stroke-width="2" />';
          });
        }
        html += '<rect x="' + pad.left + '" y="' + pad.top + '" width="' + plotW + '" height="' + plotH + '" fill="none" stroke="#cbd5e1" stroke-width="1" />';
        svg.innerHTML = html;

        if (tooltip) {
          const chartRect = () => svg.getBoundingClientRect();
          const colorNames = (options && options.datasetLabels) || [];
          svg.onmousemove = (event) => {
            const rect = chartRect();
            const relX = event.clientX - rect.left;
            const normX = Math.max(pad.left, Math.min(width - pad.right, (relX / rect.width) * width));
            const index = Math.max(0, Math.min(labels.length - 1, Math.round(((normX - pad.left) / plotW) * (labels.length - 1))));
            const lines = [];
            lines.push('<strong>' + (options && options.tooltipDates ? options.tooltipDates[index] : labels[index]) + '</strong>');
            datasets.forEach((ds, dsIdx) => {
              const value = ds.values ? ds.values[index] : null;
              if (value === null || value === undefined || Number.isNaN(value)) return;
              const label = colorNames[dsIdx] || ('Serie ' + (dsIdx + 1));
              lines.push(label + ': ' + Number(value).toFixed(2));
            });
            tooltip.innerHTML = lines.join('<br>');
            tooltip.style.display = 'block';
            tooltip.style.left = Math.min(rect.width - 140, Math.max(8, event.offsetX + 14)) + 'px';
            tooltip.style.top = Math.max(8, event.offsetY + 14) + 'px';
          };
          svg.onmouseleave = () => {
            tooltip.style.display = 'none';
          };
        }
      }

      renderLineChart('chart-qout', {{ profile_labels|tojson }}, [
        { color: '#ef6c00', values: {{ profile_qout_weekday_ia|tojson }} },
        { color: '#2563eb', values: {{ profile_qout_weekend_ia|tojson }} },
        { color: '#00838f', values: {{ prev_day_qout_real|tojson }} }
      ], {
        verticalLines: {{ chart_qout_vertical_lines|tojson }},
        tooltipDates: {{ profile_labels|tojson }},
        datasetLabels: ['IA lunes a viernes', 'IA fin de semana', 'Real dia anterior']
      });

      renderLineChart('chart-vol', {{ day_profile_labels|tojson }}, [
        { color: '#ef6c00', values: {{ day_projection_vol_ia|tojson }} },
        { color: '#6b7280', values: {{ day_projection_vol_ideal_ia|tojson }} },
        { color: '#2e7d32', values: {{ real_today_vol|tojson }} }
      ], {
        horizontal: {{ target_vol|tojson }},
        minY: 0,
        maxY: {{ chart_vol_max_y|tojson }},
        bandMin: {{ volumen_banda_min|tojson }},
        bandMax: {{ volumen_banda_max|tojson }},
        verticalLines: {{ chart_vol_vertical_lines|tojson }},
        datasetLabels: ['Vol IA', 'Vol ideal IA', 'Vol real'],
        tooltipDates: {{ day_profile_tooltip_dates|tojson }}
      });

      renderLineChart('chart-hist', {{ hist_labels|tojson }}, [
        { color: '#2e7d32', values: {{ hist_qin|tojson }} },
        { color: '#2563eb', values: {{ hist_qout|tojson }} }
      ]);
    </script>
  {% endif %}
</body>
</html>
"""


CONFIG_TEMPLATE = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Editar configuración {{ nombre }}</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 2rem; background: #f5f5f5; color: #222; }
    .card { background:#fff; border:1px solid #d6d6d6; border-radius:8px; padding:1rem; max-width: 620px; }
    .btn { display:inline-block; padding:0.4rem 0.8rem; background:#1976d2; color:#fff; text-decoration:none; border-radius:4px; border:none; cursor:pointer; }
    .btn2 { display:inline-block; padding:0.35rem 0.7rem; background:#388e3c; color:#fff; border-radius:4px; border:none; cursor:pointer; }
    .btn2:hover { background:#2e7d32; }
    label { display:block; margin-top:0.8rem; font-weight: bold; }
    input[type=text], input[type=number] { width:100%; padding:0.4rem 0.6rem; margin-top:0.25rem; box-sizing:border-box; border:1px solid #bdbdbd; border-radius:4px; background:#fff; }
    select { width:100%; padding:0.4rem 0.6rem; margin-top:0.25rem; box-sizing:border-box; border:1px solid #bdbdbd; border-radius:4px; background:#fff; }
    .row { margin-top: 1rem; display:flex; gap: 0.75rem; align-items: center; }
    .muted { color:#555; font-size: 0.9rem; }
    .suggest-wrap { position: relative; }
    .suggest-box { position:absolute; left:0; right:0; top:100%; background:#fff; border:1px solid #cfcfcf; border-top:none; z-index:10; max-height:180px; overflow:auto; display:none; }
    .suggest-item { padding:0.35rem 0.55rem; cursor:pointer; font-size:0.82rem; }
    .suggest-item:hover { background:#eef5ff; }
  </style>
</head>
<body>
  <a href="{{ url_for('index') }}" class="btn">Volver</a>
  <h1>Editar configuración: {{ nombre }}</h1>

  <div class="card">
    <form method="post" action="{{ url_for('editar_recinto_config', nombre=nombre) }}">
      <label>Point caudal entrada (qin1)</label>
      <div class="suggest-wrap">
        <input class="point-source-input" type="text" name="point_qin1" value="{{ point_qin1 or '' }}" required autocomplete="off">
        <div class="suggest-box"></div>
      </div>

      <label>Point caudal entrada (qin2) (opcional)</label>
      <div class="suggest-wrap">
        <input class="point-source-input" type="text" name="point_qin2" value="{{ point_qin2 or '' }}" autocomplete="off">
        <div class="suggest-box"></div>
      </div>

      <label>Point volumen (vol)</label>
      <div class="suggest-wrap">
        <input class="point-source-input" type="text" name="point_vol" value="{{ point_vol or '' }}" required autocomplete="off">
        <div class="suggest-box"></div>
      </div>

      <label>Point caudal salida (qout)</label>
      <div class="suggest-wrap">
        <input class="point-source-input" type="text" name="point_qout" value="{{ point_qout or '' }}" required autocomplete="off">
        <div class="suggest-box"></div>
      </div>

      <label>Volumen máximo estanque (m3)</label>
      <input type="number" step="0.01" name="volumen_maximo" value="{{ volumen_maximo if volumen_maximo is not none else '' }}">

      <label>Unidad caudal salida (qout)</label>
      <select name="qout_unit">
        <option value="l/s" {% if qout_unit == 'l/s' %}selected{% endif %}>l/s</option>
        <option value="m3/hr" {% if qout_unit == 'm3/hr' %}selected{% endif %}>m3/hr</option>
      </select>

      <div class="row">
        <label style="margin:0; font-weight: normal;">
          <input type="checkbox" name="activo" value="1" {% if activo %}checked{% endif %}>
          Activo
        </label>
      </div>

      <div class="row">
        <button type="submit" class="btn2">Guardar configuración</button>
      </div>

      <p class="muted">
        Para recalcular en el SQLite, usa el botón <b>full</b> o <b>append</b> desde la tabla de recintos.
      </p>
    </form>
  </div>
  <script>
    async function wirePointSuggestions(root) {
      const inputs = root.querySelectorAll('.point-source-input');
      inputs.forEach(input => {
        const box = input.parentElement.querySelector('.suggest-box');
        if (!box) return;
        input.addEventListener('input', async () => {
          const q = input.value.trim();
          if (!q || /^[0-9]+$/.test(q) || q.length < 2) {
            box.style.display = 'none';
            box.innerHTML = '';
            return;
          }
          const res = await fetch('/recintos/api/point-suggest?q=' + encodeURIComponent(q));
          const data = await res.json();
          box.innerHTML = (data.items || []).map(item => '<div class="suggest-item" data-tag="' + item.tag + '">' + item.tag + (item.descripcion ? ' | ' + item.descripcion : '') + (item.source ? ' [' + item.source + ']' : '') + '</div>').join('');
          box.style.display = box.innerHTML ? 'block' : 'none';
          box.querySelectorAll('.suggest-item').forEach(el => {
            el.addEventListener('mousedown', (ev) => {
              ev.preventDefault();
              input.value = el.dataset.tag || '';
              box.style.display = 'none';
              box.innerHTML = '';
            });
          });
        });
        input.addEventListener('blur', () => setTimeout(() => { box.style.display = 'none'; }, 120));
      });
    }
    wirePointSuggestions(document);
  </script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    recintos_cfg = load_all_recintos()
    recintos_view = []
    editing_name = request.args.get("edit")
    for r in recintos_cfg:
        f_ini = get_sqlite_min_fecha(r.nombre)
        f_fin = get_sqlite_last_fecha(r.nombre)
        recintos_view.append(
            type(
                "RView",
                (),
                {
                    "nombre": r.nombre,
                    "tag_qin1": r.tag_qin1,
                    "tag_qin2": r.tag_qin2,
                    "tag_vol": r.tag_vol,
                    "tag_qout": r.tag_qout,
                    "point_qin1": r.point_qin1,
                    "point_qin2": r.point_qin2,
                    "point_vol": r.point_vol,
                    "point_qout": r.point_qout,
                    "source_type": r.source_type,
                    "volumen_maximo": r.volumen_maximo,
                    "activo": r.activo,
                    "last_run_at": r.last_run_at,
                    "ia_recent": is_recent_training(r.nombre),
                    "qout_unit": r.qout_unit,
                    "fecha_ini_sqlite": f_ini.isoformat(timespec="minutes") if f_ini else None,
                    "fecha_fin_sqlite": f_fin.isoformat(timespec="minutes") if f_fin else None,
                    "fecha_fin_estado": fecha_fin_sqlite_estado(f_fin),
                    "total_rows": get_sqlite_row_count(r.nombre),
                },
            )
        )
    status_msg = request.args.get("status_msg")
    default_t_ini = datetime(2026, 1, 1, 0, 0)
    default_t_ini_iso = default_t_ini.isoformat(timespec="minutes")
    default_t_fin_iso = datetime.now().replace(second=0, microsecond=0).isoformat(timespec="minutes")
    return render_template_string(
        INDEX_TEMPLATE,
        recintos=recintos_view,
        status_msg=status_msg,
        default_t_ini=default_t_ini_iso,
        default_t_fin=default_t_fin_iso,
        editing_name=editing_name,
    )


@app.route("/recintos/api/point-suggest", methods=["GET"])
def point_suggest_api():
    q = request.args.get("q", "")
    if (q or "").strip().isdigit():
        items = search_mysql_legacy_tag_suggestions(q)
    else:
        items = search_point_suggestions(q)
        for item in items:
            item.setdefault("source", "influxdb")
    return jsonify({"items": items})


@app.route("/correlacion", methods=["GET"])
def correlacion_index():
    init_corr_db()
    status_msg = request.args.get("status_msg")
    selected_name = request.args.get("project")
    projects = list_corr_projects()
    active_project = next((p for p in projects if p["nombre"] == selected_name), None)
    default_t_ini = datetime(2026, 1, 1, 0, 0).isoformat(timespec="minutes")
    default_t_fin = datetime.now().replace(second=0, microsecond=0).isoformat(timespec="minutes")
    return render_template_string(
        CORR_TEMPLATE,
        status_msg=status_msg,
        projects=projects,
        active_project=active_project,
        default_t_ini=default_t_ini,
        default_t_fin=default_t_fin,
    )


@app.route("/correlacion/resultados", methods=["GET"])
def correlacion_results_index():
    init_corr_db()
    projects = list_corr_projects()
    selected_name = (request.args.get("project") or "").strip()
    active_project = next((p for p in projects if p["nombre"] == selected_name), None)
    outdir_raw = (request.args.get("outdir") or "").strip()
    if outdir_raw:
        outdir = Path(outdir_raw)
    else:
        candidate = _default_corr_results_dir(active_project["nombre"] if active_project else None)
        default_outdir = _default_corr_results_dir()
        outdir = candidate if candidate.exists() else default_outdir
    result = load_corr1_outputs(outdir)
    outdir_compare_raw = (request.args.get("outdir_compare") or "").strip()
    compare_result = None
    compare = None
    if outdir_compare_raw:
        compare_result = load_corr1_outputs(Path(outdir_compare_raw))
        compare = build_corr1_comparison(result, compare_result)
    series_tags_raw = (request.args.get("series_tags") or "").strip()
    series_limit = int(request.args.get("series_limit") or 192)
    if series_limit < 48:
        series_limit = 48
    if series_limit > 1000:
        series_limit = 1000
    if series_tags_raw:
        selected_series = [part.strip() for part in series_tags_raw.split(",") if part.strip()]
    else:
        selected_series = [
            "cp_escorial_entrada_caudal",
            "cp_rosario_consumoxhr",
            "cp_nivel_tk_rosario2",
        ]
    sqlite_for_series = None
    if result.get("metrics") and (result["metrics"] or {}).get("sqlite"):
        sqlite_for_series = str((result["metrics"] or {}).get("sqlite"))
    elif active_project:
        sqlite_for_series = str(active_project.get("sqlite_file") or "")
    series_analysis = load_corr_timeseries_analysis(
        sqlite_path=sqlite_for_series,
        selected_columns=selected_series,
        limit=series_limit,
        corr_window=12,
    )
    return render_template_string(
        CORR_RESULTS_TEMPLATE,
        projects=projects,
        selected_project=active_project,
        selected_project_name=selected_name,
        outdir=str(outdir),
        outdir_compare=outdir_compare_raw,
        result=result,
        compare_result=compare_result,
        compare=compare,
        series_tags=",".join(selected_series),
        series_limit=series_limit,
        series_analysis=series_analysis,
    )


@app.route("/correlacion/api/taginfo", methods=["GET"])
def correlacion_taginfo_api():
    raw = (request.args.get("tagid") or "").strip()
    if not raw.isdigit():
        return jsonify({"tagid": raw, "codigo_tag": None})
    try:
        codigo = get_mysql_legacy_codigo_tag_by_id(int(raw))
        return jsonify({"tagid": int(raw), "codigo_tag": codigo})
    except Exception:
        return jsonify({"tagid": int(raw), "codigo_tag": None})


@app.route("/correlacion/proyectos/save", methods=["POST"])
def correlacion_save_project():
    nombre = (request.form.get("nombre") or "").strip()
    fecha_ini_raw = (request.form.get("fecha_ini") or "").strip()
    fecha_fin_raw = (request.form.get("fecha_fin") or "").strip()
    tagids = request.form.getlist("tagid[]")
    codigos = request.form.getlist("codigo_tag[]")
    tipos = request.form.getlist("medidor_tipo[]")
    try:
        fecha_ini = _parse_dt_local(fecha_ini_raw)
        fecha_fin = _parse_dt_local(fecha_fin_raw)
        tags: List[Dict[str, object]] = []
        for i in range(min(len(tagids), len(codigos), len(tipos))):
            if not (tagids[i] or "").strip():
                continue
            tagid_int = int(tagids[i])
            codigo = (codigos[i] or "").strip() or (get_mysql_legacy_codigo_tag_by_id(tagid_int) or "")
            if not codigo:
                raise ValueError(f"No se encontro codigo_tag para tagid {tagid_int}")
            tags.append(
                {
                    "tagid": tagid_int,
                    "codigo_tag": codigo,
                    "medidor_tipo": (tipos[i] or "otro").strip(),
                }
            )
        save_corr_project(nombre, fecha_ini, fecha_fin, tags)
        return redirect(url_for("correlacion_index", project=nombre, status_msg="Proyecto guardado"))
    except Exception as exc:
        return redirect(url_for("correlacion_index", status_msg=f"Error guardando proyecto: {exc}"))


@app.route("/correlacion/proyectos/<nombre>/build", methods=["POST"])
def correlacion_build_project(nombre: str):
    try:
        sqlite_file, rows = build_corr_sqlite(nombre)
        return redirect(
            url_for(
                "correlacion_index",
                project=nombre,
                status_msg=f"SQLite generado OK: {sqlite_file} ({rows} filas)",
            )
        )
    except Exception as exc:
        return redirect(url_for("correlacion_index", project=nombre, status_msg=f"Error generando SQLite: {exc}"))


@app.route("/correlacion/proyectos/<nombre>/delete", methods=["POST"])
def correlacion_delete_project(nombre: str):
    init_corr_db()
    conn = sqlite3.connect(RT3_IA_CORR_DB)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM corr_projects WHERE nombre = ?", (nombre,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("correlacion_index", status_msg=f"Proyecto eliminado: {nombre}"))


@app.route("/correlacion/proyectos/<nombre>/duplicate", methods=["POST"])
def correlacion_duplicate_project(nombre: str):
    projects = {p["nombre"]: p for p in list_corr_projects()}
    p = projects.get(nombre)
    if not p:
        return redirect(url_for("correlacion_index", status_msg="Proyecto no encontrado"))
    copy_name = f"{nombre}_copia"
    suffix = 1
    while copy_name in projects:
        suffix += 1
        copy_name = f"{nombre}_copia_{suffix}"
    try:
        save_corr_project(
            copy_name,
            datetime.fromisoformat(str(p["fecha_ini"])),
            datetime.fromisoformat(str(p["fecha_fin"])),
            list(p["tags"]),
        )
        return redirect(url_for("correlacion_index", project=copy_name, status_msg=f"Proyecto duplicado: {copy_name}"))
    except Exception as exc:
        return redirect(url_for("correlacion_index", status_msg=f"Error duplicando proyecto: {exc}"))


def _parse_dt_local(val: str) -> datetime:
    # formato típico de input datetime-local: "YYYY-MM-DDTHH:MM"
    return datetime.strptime(val, "%Y-%m-%dT%H:%M")


def normalize_qout_unit(raw: Optional[str]) -> str:
    """
    Convierte el valor recibido desde UI/API a uno de:
    - "l/s"
    - "m3/hr"
    """
    if not raw:
        return "l/s"
    v = str(raw).strip().lower().replace(" ", "")
    if v in {"m3/hr", "m3h", "m3hr", "qout-m3", "qout_m3", "qoutm3"}:
        return "m3/hr"
    return "l/s"


def check_recinto_source_connection(cfg: RecintoConfig) -> Tuple[bool, str]:
    if cfg.source_type == "mysql_legacy":
        try:
            conn_test = get_mysql_connection()
            conn_test.close()
            return True, ""
        except Exception as exc:
            return False, f"Error conectando a MySQL legacy: {exc}"
    return True, ""


def run_append_for_recinto(
    cfg_or_nombre: RecintoConfig | str,
    paso_min: int = 15,
    now_dt: Optional[datetime] = None,
    source_label: str = "append automático",
) -> Dict[str, object]:
    cfg = cfg_or_nombre if isinstance(cfg_or_nombre, RecintoConfig) else load_recinto_config(str(cfg_or_nombre).strip())
    if not cfg:
        return {"ok": False, "status": "error", "status_msg": f"Recinto {cfg_or_nombre} no encontrado."}

    conn_ok, err_msg = check_recinto_source_connection(cfg)
    if not conn_ok:
        return {"ok": False, "status": "error", "status_msg": err_msg}

    context_start = get_sqlite_context_start_for_append(cfg.nombre)
    repair_start = get_sqlite_missing_context_start_for_repair(cfg.nombre)
    candidate_starts = [dt for dt in (context_start, repair_start) if dt is not None]
    if not candidate_starts:
        return {
            "ok": False,
            "status": "error",
            "status_msg": f"No hay datos previos en SQLite para {cfg.nombre}. Use 'Guardar y procesar' con un rango inicial.",
        }

    t_ini_effective = min(candidate_starts)
    t_fin = now_dt or datetime.now()
    if t_ini_effective >= t_fin:
        return {
            "ok": True,
            "status": "noop",
            "status_msg": (
                f"No hay rango nuevo para append en {cfg.nombre}. "
                f"Última fecha SQLite: {t_ini_effective.isoformat(timespec='minutes')}, "
                f"ahora: {t_fin.isoformat(timespec='minutes')}."
            ),
            "cfg": cfg,
        }

    series = calcular_series_recinto(cfg, t_ini_effective, t_fin, int(paso_min))
    series = merge_append_series_preserving_existing(cfg.nombre, series)
    total_bins = len(series)
    n_rows = insert_medidas_batch(cfg.nombre, series)

    cfg.last_run_at = datetime.now().isoformat(timespec="minutes")
    cfg.last_rows_saved = n_rows
    save_recinto_config(cfg)

    repaired_txt = " con reparacion de vacios detectados" if repair_start is not None else ""
    status_msg = (
        f"Modo APPEND ({source_label}){repaired_txt}. Conexión {cfg.source_type} OK. "
        f"Procesando {total_bins} intervalos de {int(paso_min)} min "
        f"desde {t_ini_effective.isoformat(timespec='minutes')} hasta {t_fin.isoformat(timespec='minutes')}. "
        f"Fin de proceso, {n_rows} registros guardados/actualizados en SQLite para recinto {cfg.nombre}."
    )
    return {
        "ok": True,
        "status": "ok",
        "status_msg": status_msg,
        "cfg": cfg,
        "rows_saved": n_rows,
        "total_bins": total_bins,
        "t_ini_effective": t_ini_effective,
        "t_fin": t_fin,
    }


def infer_source_from_field(value: Optional[str]) -> Tuple[Optional[int], Optional[str], str]:
    raw = (value or "").strip()
    if not raw:
        return None, None, "influxdb"
    if raw.isdigit():
        return int(raw), None, "mysql_legacy"
    return None, raw, "influxdb"


def parse_m3hr_expression(value: Optional[str]) -> Optional[List[str]]:
    raw = (value or "").strip()
    if not raw:
        return None
    match = re.match(r"^m3hr\s*\(\s*(.+?)\s*\)$", raw, flags=re.IGNORECASE)
    if not match:
        return None
    inner = (match.group(1) or "").strip()
    parts: List[str] = []
    current: List[str] = []
    in_quote: Optional[str] = None
    brace_depth = 0
    for ch in inner:
        if ch in ("'", '"'):
            if in_quote == ch:
                in_quote = None
            elif in_quote is None:
                in_quote = ch
        elif ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth = max(0, brace_depth - 1)
        if ch == "," and in_quote is None and brace_depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(ch)
    part = "".join(current).strip()
    if part:
        parts.append(part)

    normalized: List[str] = []
    for item in parts:
        token = item.strip()
        if token.startswith("{") and token.endswith("}"):
            token = token[1:-1].strip()
        if (token.startswith('"') and token.endswith('"')) or (token.startswith("'") and token.endswith("'")):
            token = token[1:-1].strip()
        if token:
            normalized.append(token)
    return normalized or None


def search_point_suggestions(query: str, limit: int = 10) -> List[Dict[str, str]]:
    q = (query or "").strip()
    if not q or RT3_DB_PATH.exists() is False:
        return []
    conn = sqlite3.connect(str(RT3_DB_PATH))
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        pattern = f"%{q}%"
        cur.execute(
            """
            SELECT tag, descripcion
            FROM point
            WHERE tag LIKE ? OR descripcion LIKE ?
            ORDER BY tag
            LIMIT ?
            """,
            (pattern, pattern, int(limit)),
        )
        return [
            {
                "tag": (row["tag"] or "").strip(),
                "descripcion": (row["descripcion"] or "").strip(),
            }
            for row in cur.fetchall()
        ]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def search_mysql_legacy_tag_suggestions(query: str) -> List[Dict[str, str]]:
    q = (query or "").strip()
    if not q or not q.isdigit():
        return []
    conn = None
    try:
        conn = get_mysql_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, codigo_tags
            FROM rtstruct.tag
            WHERE id = %s
            LIMIT 1
            """,
            (int(q),),
        )
        row = cur.fetchone()
        if not row:
            return []
        tag_id, codigo_tags = row
        return [
            {
                "tag": str(tag_id),
                "descripcion": str(codigo_tags or ""),
                "source": "mysql_legacy",
            }
        ]
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.route("/recintos", methods=["POST"])
def crear_recinto():
    """
    Crea/actualiza recinto y dispara cálculo para el rango indicado.
    Acepta:
    - form-data desde el formulario HTML (modo FULL siempre)
    - JSON: {nombre, tag_qin1, tag_qin2, tag_vol, tag_qout, t_ini, t_fin, paso_min, accion}
      donde accion puede ser "full" (recalcular todo) o "append" (agregar desde último dato).
    """
    if request.is_json:
        data = request.get_json(force=True)
        nombre = data.get("nombre")
        tag_qin1, point_qin1, source_qin1 = infer_source_from_field(data.get("point_qin1") or data.get("tag_qin1") or data.get("tag_qin"))
        tag_qin2, point_qin2, source_qin2 = infer_source_from_field(data.get("point_qin2") or data.get("tag_qin2"))
        tag_vol, point_vol, source_vol = infer_source_from_field(data.get("point_vol") or data.get("tag_vol"))
        tag_qout, point_qout, source_qout = infer_source_from_field(data.get("point_qout") or data.get("tag_qout"))
        source_candidates = [source_qin1, source_qin2, source_vol, source_qout]
        source_type = "influxdb" if "influxdb" in source_candidates else "mysql_legacy"
        volumen_maximo = safe_float(data.get("volumen_maximo"))
        t_ini_str = data.get("t_ini")  # ISO "YYYY-MM-DDTHH:MM"
        t_fin_str = data.get("t_fin")
        paso_min = int(data.get("paso_min") or 15)
        t_ini = _parse_dt_local(t_ini_str.replace(" ", "T")) if "T" not in t_ini_str else _parse_dt_local(t_ini_str)
        t_fin = _parse_dt_local(t_fin_str.replace(" ", "T")) if "T" not in t_fin_str else _parse_dt_local(t_fin_str)
        accion = data.get("accion") or "full"
        qout_unit = normalize_qout_unit(data.get("qout_unit") or data.get("qout-m3"))
    else:
        nombre = request.form["nombre"]
        tag_qin1, point_qin1, source_qin1 = infer_source_from_field(request.form.get("point_qin1"))
        tag_qin2, point_qin2, source_qin2 = infer_source_from_field(request.form.get("point_qin2"))
        tag_vol, point_vol, source_vol = infer_source_from_field(request.form.get("point_vol"))
        tag_qout, point_qout, source_qout = infer_source_from_field(request.form.get("point_qout"))
        source_candidates = [source_qin1, source_qin2, source_vol, source_qout]
        source_type = "influxdb" if "influxdb" in source_candidates else "mysql_legacy"
        volumen_maximo = safe_float(request.form.get("volumen_maximo"))
        t_ini = _parse_dt_local(request.form["t_ini"])
        t_fin = _parse_dt_local(request.form["t_fin"])
        paso_min = int(request.form.get("paso_min") or 15)
        accion = "full"  # desde formulario siempre es recalcular todo
        qout_unit = normalize_qout_unit(request.form.get("qout_unit"))

    cfg = RecintoConfig(
        nombre=nombre.strip(),
        tag_qin1=tag_qin1,
        tag_qin2=tag_qin2,
        tag_vol=tag_vol,
        tag_qout=tag_qout,
        point_qin1=point_qin1,
        point_qin2=point_qin2,
        point_vol=point_vol,
        point_qout=point_qout,
        source_type=source_type,
        volumen_maximo=volumen_maximo,
        qout_unit=qout_unit,
    )
    # Guardar configuración básica primero
    save_recinto_config(cfg)

    # Mensajes de estado
    # 1) probar conexión a la fuente seleccionada
    if cfg.source_type == "mysql_legacy":
        try:
            conn_test = get_mysql_connection()
            conn_test.close()
            conn_ok = True
        except Exception as exc:
            conn_ok = False
            err_msg = f"Error conectando a MySQL legacy: {exc}"
    else:
        conn_ok = True
        err_msg = ""

    if not conn_ok:
        if request.is_json:
            return jsonify({"status": "error", "message": err_msg}), 500
        return redirect(url_for("index", status_msg=err_msg))

    # 2) determinar rango y modo según acción
    append_mode = accion == "append"
    if append_mode:
        # Tomar un rango con contexto desde la última fecha NO-vacía
        # al final del SQLite, para rellenar bins vacíos aunque el append
        # se ejecute en el borde de 15 min.
        context_start = get_sqlite_context_start_for_append(cfg.nombre)
        repair_start = get_sqlite_missing_context_start_for_repair(cfg.nombre)
        candidate_starts = [dt for dt in (context_start, repair_start, t_ini) if dt is not None]
        t_ini_effective = min(candidate_starts)
    else:
        # modo full: limpiar SQLite y usar t_ini proporcionado
        clear_recinto_medidas(cfg.nombre)
        t_ini_effective = t_ini

    # 3) cálculo e inserción
    series = calcular_series_recinto(cfg, t_ini_effective, t_fin, paso_min)
    if append_mode:
        series = merge_append_series_preserving_existing(cfg.nombre, series)
    total_bins = len(series)
    n_rows = insert_medidas_batch(cfg.nombre, series)

    # actualizar info de última migración
    cfg.last_run_at = datetime.now().isoformat(timespec="minutes")
    cfg.last_rows_saved = n_rows
    save_recinto_config(cfg)

    if request.is_json:
        return jsonify(
            {
                "status": "ok",
                "recinto": cfg.nombre,
                "rows_saved": n_rows,
                "total_bins": total_bins,
                "mode": "append" if append_mode else "full",
                "message": (
                    (
                        "Modo APPEND: " if append_mode else "Modo FULL: "
                    )
                    + f"Conexión {cfg.source_type} OK. Procesando {total_bins} intervalos de {paso_min} min "
                    f"desde {t_ini_effective.isoformat(timespec='minutes')} hasta {t_fin.isoformat(timespec='minutes')}. "
                    f"Fin de proceso, {n_rows} registros guardados/actualizados en SQLite."
                ),
                "db_path": str(get_recinto_db_path(cfg.nombre)),
            }
        )

    modo_txt = "APPEND (agregar desde último dato)" if append_mode else "FULL (recalcular todo)"
    status_msg = (
        f"Modo {modo_txt}. Conexión {cfg.source_type} OK. Procesando {total_bins} intervalos de {paso_min} min "
        f"desde {t_ini_effective.isoformat(timespec='minutes')} hasta {t_fin.isoformat(timespec='minutes')}. "
        f"Fin de proceso, {n_rows} registros guardados/actualizados en SQLite para recinto {cfg.nombre}."
    )
    return redirect(url_for("index", status_msg=status_msg))


@app.route("/recintos/<nombre>/toggle", methods=["POST"])
def toggle_recinto(nombre: str):
    """
    Activa / desactiva un recinto desde la tabla HTML.
    """
    valor = request.form.get("activo")
    activo = bool(int(valor)) if valor is not None else True
    set_recinto_activo(nombre.strip(), activo)
    return redirect(url_for("index"))


@app.route("/recintos/<nombre>/ver", methods=["GET"])
def ver_recinto(nombre: str):
    """
    Muestra tabla y gráfico de los datos del SQLite de un recinto.
    Por simplicidad, muestra todos los registros (o se podría limitar a los últimos N).
    """
    cfg = load_recinto_config(nombre.strip())
    if not cfg:
        return redirect(url_for("index", status_msg=f"Recinto {nombre} no encontrado."))

    db_path = get_recinto_db_path(cfg.nombre)
    filas = []

    if db_path.exists():
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT fecha, qin, vol, qout FROM medidas ORDER BY fecha DESC")
            for fecha, qin, vol, qout in cur.fetchall():
                filas.append(
                    type(
                        "Fila",
                        (),
                        {
                            "fecha": fecha,
                            "qin": qin,
                            "vol": vol,
                            "qout": qout,
                        },
                    )
                )
        finally:
            conn.close()

    return render_template_string(DETAIL_TEMPLATE, nombre=cfg.nombre, filas=filas)


@app.route("/recintos/<nombre>/ia", methods=["GET"])
def ver_recinto_ia(nombre: str):
    cfg = load_recinto_config(nombre.strip())
    if not cfg:
        return redirect(url_for("index", status_msg=f"Recinto {nombre} no encontrado."))

    now_chile = datetime.now(CHILE_TZ)
    chile_today = now_chile.replace(tzinfo=None)
    default_profile_group = profile_group_for_chile_day(chile_today)
    selected_profile_group = (request.args.get("profile_group") or default_profile_group).strip().lower()
    if selected_profile_group not in {"weekday", "weekend"}:
        selected_profile_group = default_profile_group
    sqlite_path = str(get_recinto_db_path(cfg.nombre))
    perfil = load_perfil_consumo(cfg.nombre, profile_type=selected_profile_group)
    perfil_weekday = load_perfil_consumo(cfg.nombre, profile_type="weekday")
    perfil_weekend = load_perfil_consumo(cfg.nombre, profile_type="weekend")
    meta = load_perfil_metadata(cfg.nombre)
    ultima = get_sqlite_last_medida(cfg.nombre)
    ultima_qin = get_sqlite_last_qin(cfg.nombre)
    max_vol_ideal = get_sqlite_max_volumen(cfg.nombre)
    max_qin_historico = get_sqlite_max_qin(cfg.nombre)
    chart_vol_max_y = cfg.volumen_maximo if cfg.volumen_maximo is not None else (max_vol_ideal if max_vol_ideal is not None else 500.0)
    if chart_vol_max_y <= 0:
        chart_vol_max_y = 500.0
    volumen_banda_min = None
    volumen_banda_max = None
    if cfg.volumen_maximo is not None and cfg.volumen_maximo > 0:
        volumen_banda_min = cfg.volumen_maximo * 0.50
        volumen_banda_max = cfg.volumen_maximo * 0.90
    recent = get_sqlite_recent_medidas(cfg.nombre, limit=96)

    last_fecha = ultima[0].isoformat(timespec="minutes") if ultima is not None else None
    last_qin = ultima_qin[1] if ultima_qin is not None else (ultima[1] if ultima is not None else None)
    last_qin_fecha = ultima_qin[0].isoformat(timespec="minutes") if ultima_qin is not None else (last_fecha if last_qin is not None else None)
    last_vol = ultima[2] if ultima is not None else None

    qin_default = 0.0
    if last_qin is not None:
        qin_default = float(last_qin)
    qin_fijo = safe_float(request.args.get("qin_fijo"), qin_default)
    if qin_fijo is None:
        qin_fijo = qin_default
    vol_view = request.args.get("vol_view", "today")
    if vol_view not in {"today", "prev7"}:
        vol_view = "today"

    target_vol_input_raw = request.args.get("target_vol")
    default_target_vol = volumen_banda_max if volumen_banda_max is not None else max_vol_ideal
    if target_vol_input_raw in (None, ""):
        target_vol = default_target_vol
        target_vol_input = ""
    else:
        target_vol = safe_float(target_vol_input_raw, default_target_vol)
        target_vol_input = target_vol_input_raw

    projection_rows = build_projection_24h(cfg.nombre, profile_type=selected_profile_group)
    projection_avg = simulate_volume_projection(last_vol, qin_fijo, projection_rows, "qout_promedio")
    projection_ia = simulate_volume_projection(last_vol, qin_fijo, projection_rows, "qout_ia")
    qin_requerido = compute_required_fixed_qin(last_vol, target_vol, projection_rows, "qout_ia")
    qin_ideal_sugerido = compute_constrained_qin_ideal(
        last_vol,
        target_vol,
        projection_rows,
        "qout_ia",
        max_qin_historico,
    )
    yesterday = chile_today - timedelta(days=1)
    current_chile_slot_float = (
        now_chile.hour * 4
        + (now_chile.minute / 15.0)
        + (now_chile.second / 900.0)
    )
    today_rows = get_sqlite_day_medidas(cfg.nombre, chile_today)
    initial_day_volume = None
    latest_today_dt = None
    if today_rows:
        for dt, _, vol, _ in reversed(today_rows):
            if vol is not None:
                initial_day_volume = float(vol)
                latest_today_dt = dt
                break
    if initial_day_volume is None:
        initial_day_volume = last_vol
        latest_today_dt = ultima[0] if ultima is not None else None
    day_start = chile_today.replace(hour=0, minute=0, second=0, microsecond=0)
    day_profile_labels, day_profile_tooltip_dates, extended_profile = build_extended_profile_window(perfil, day_start, extra_hours=12)
    latest_today_slot = slot_index_from_datetime(latest_today_dt) if latest_today_dt is not None else 0
    projection_start_slot = min(len(extended_profile) - 1, max(latest_today_slot, int(math.ceil(current_chile_slot_float))))
    projection_start_dt = day_start + timedelta(minutes=15 * projection_start_slot)
    projection_start_volume_avg = advance_day_volume_to_slot(
        initial_day_volume,
        qin_fijo,
        extended_profile,
        "qout_promedio",
        latest_today_slot,
        projection_start_slot,
    )
    projection_start_volume_ia = advance_day_volume_to_slot(
        initial_day_volume,
        qin_fijo,
        extended_profile,
        "qout_ia",
        latest_today_slot,
        projection_start_slot,
    )
    qin_evento = float(qin_fijo)
    projection_start_volume_evento = advance_day_volume_to_slot(
        initial_day_volume,
        qin_evento,
        extended_profile,
        "qout_ia",
        latest_today_slot,
        projection_start_slot,
    )
    volumen_rebalse = cfg.volumen_maximo if cfg.volumen_maximo is not None and cfg.volumen_maximo > 0 else None
    volumen_bajo_10 = (cfg.volumen_maximo * 0.10) if cfg.volumen_maximo is not None and cfg.volumen_maximo > 0 else None
    rebalse_event = find_volume_threshold_event(
        projection_start_volume_evento,
        qin_evento,
        perfil,
        "qout_ia",
        projection_start_dt,
        volumen_rebalse,
        "above",
    )
    bajo_10_event = find_volume_threshold_event(
        projection_start_volume_evento,
        qin_evento,
        perfil,
        "qout_ia",
        projection_start_dt,
        volumen_bajo_10,
        "below",
    )
    reference_now_dt = now_chile.replace(tzinfo=None)
    rebalse_card = build_volume_event_card(
        "Rebalse con qin proyectado",
        rebalse_event,
        reference_now_dt,
        qin_evento,
        "qin proyectado",
    )
    bajo_10_card = build_volume_event_card(
        "Bajo 10% con qin proyectado",
        bajo_10_event,
        reference_now_dt,
        qin_evento,
        "qin proyectado",
    )
    day_projection_vol_avg = simulate_day_volume_projection_from_slot(
        projection_start_volume_avg,
        qin_fijo,
        extended_profile,
        "qout_promedio",
        projection_start_slot,
    )
    day_projection_vol_ia = simulate_day_volume_projection_from_slot(
        projection_start_volume_ia,
        qin_fijo,
        extended_profile,
        "qout_ia",
        projection_start_slot,
    )
    day_projection_vol_ideal_ia = build_sinusoidal_ideal_volume_series(
        extended_profile,
        "qout_ia",
        volumen_banda_min,
        volumen_banda_max,
    )
    today_vol_by_slot = {
        slot_index_from_datetime(dt): (float(vol) if vol is not None else None)
        for dt, _, vol, _ in today_rows
    }
    real_today_vol = [today_vol_by_slot.get(slot) if slot < 96 else None for slot in range(len(extended_profile))]
    chart_vol_vertical_lines = [
        {"index": current_chile_slot_float, "color": "#c62828", "dash": ""},
        {"index": 0, "color": "#9ca3af", "dash": "5 5"},
        {"index": 96, "color": "#9ca3af", "dash": "5 5"},
    ]

    if vol_view == "prev7":
        prev7_start = day_start - timedelta(days=7)
        prev7_end = day_start + timedelta(days=1, hours=12)
        prev7_rows = get_sqlite_range_medidas(cfg.nombre, prev7_start, prev7_end)
        total_slots_prev7 = int(((prev7_end - prev7_start).total_seconds() // 900) + 1)
        day_profile_labels = []
        day_profile_tooltip_dates = []
        real_prev7_vol_map: Dict[int, Optional[float]] = {}
        prev7_profile: List[Dict[str, Optional[float]]] = []
        perfil_by_slot = {int(row["slot_index"]): row for row in perfil}
        for dt, _, vol, _ in prev7_rows:
            slot_idx = int((dt - prev7_start).total_seconds() // 900)
            real_prev7_vol_map[slot_idx] = float(vol) if vol is not None else None
        real_today_vol = []
        day_projection_vol_avg = [None for _ in range(total_slots_prev7)]
        day_projection_vol_ia = [None for _ in range(total_slots_prev7)]
        chart_vol_vertical_lines = []
        for idx in range(total_slots_prev7):
            ts = prev7_start + timedelta(minutes=15 * idx)
            day_profile_labels.append(format_day_label(ts) if ts.hour == 0 and ts.minute == 0 else ts.strftime("%H:%M"))
            day_profile_tooltip_dates.append(ts.strftime("%Y-%m-%d %H:%M"))
            prev7_profile.append(
                {
                    "slot_index": slot_index_from_datetime(ts),
                    "hora_texto": ts.strftime("%H:%M"),
                    "qout_promedio": perfil_by_slot.get(slot_index_from_datetime(ts), {}).get("qout_promedio"),
                    "qout_ia": perfil_by_slot.get(slot_index_from_datetime(ts), {}).get("qout_ia"),
                }
            )
            real_today_vol.append(real_prev7_vol_map.get(idx))
            if ts.hour == 0 and ts.minute == 0:
                chart_vol_vertical_lines.append({"index": idx, "color": "#9ca3af", "dash": "5 5"})
        current_index_prev7 = (now_chile.replace(tzinfo=None) - prev7_start).total_seconds() / 900.0
        chart_vol_vertical_lines.append({"index": current_index_prev7, "color": "#c62828", "dash": ""})
        anchor_index_prev7 = int(((latest_today_dt - prev7_start).total_seconds() // 900)) if latest_today_dt is not None else max(0, min(total_slots_prev7 - 1, int(current_index_prev7)))
        # Para vista "7 dias anterior", proyectar en ambos sentidos
        # usando como ancla el volumen real disponible en el indice ancla.
        anchor_volume_prev7 = None
        if 0 <= anchor_index_prev7 < len(real_today_vol):
            anchor_volume_prev7 = real_today_vol[anchor_index_prev7]
        if anchor_volume_prev7 is None:
            anchor_volume_prev7 = initial_day_volume
        day_projection_vol_avg = simulate_day_volume_projection_bidirectional(
            anchor_volume_prev7,
            qin_fijo,
            prev7_profile,
            "qout_promedio",
            anchor_index_prev7,
        )
        day_projection_vol_ia = simulate_day_volume_projection_bidirectional(
            anchor_volume_prev7,
            qin_fijo,
            prev7_profile,
            "qout_ia",
            anchor_index_prev7,
        )
        projection_visible_from = min(total_slots_prev7 - 1, max(0, int(math.ceil(current_index_prev7))))
        day_projection_vol_avg = [
            value if idx >= projection_visible_from else None
            for idx, value in enumerate(day_projection_vol_avg)
        ]
        day_projection_vol_ia = [
            value if idx >= projection_visible_from else None
            for idx, value in enumerate(day_projection_vol_ia)
        ]
        day_projection_vol_ideal_ia = build_sinusoidal_ideal_volume_series(
            prev7_profile,
            "qout_ia",
            volumen_banda_min,
            volumen_banda_max,
        )
    projection_table = []
    for idx, row in enumerate(projection_rows):
        projection_table.append(
            type(
                "ProjectionRow",
                (),
                {
                    "timestamp": row["timestamp"],
                    "hora_texto": row["hora_texto"],
                    "qout_promedio": row["qout_promedio"],
                    "qout_ia": row["qout_ia"],
                    "vol_avg": projection_avg[idx]["volumen"] if idx < len(projection_avg) else None,
                    "vol_ia": projection_ia[idx]["volumen"] if idx < len(projection_ia) else None,
                },
            )
        )

    def _series_mean(key: str) -> Optional[float]:
        values = [float(row[key]) for row in projection_rows if row.get(key) is not None]
        if not values:
            return None
        return sum(values) / len(values)

    projection_labels = [row["hora_texto"] for row in projection_rows]
    projection_qout_avg = [row["qout_promedio"] for row in projection_rows]
    projection_qout_ia = [row["qout_ia"] for row in projection_rows]
    projection_vol_avg = [row["volumen"] for row in projection_avg]
    projection_vol_ia = [row["volumen"] for row in projection_ia]
    profile_chart_source = perfil_weekday if perfil_weekday else (perfil_weekend if perfil_weekend else perfil)
    profile_labels = [str(row["hora_texto"]) for row in profile_chart_source]
    profile_qout_weekday_ia = [row["qout_ia"] for row in perfil_weekday]
    profile_qout_weekend_ia = [row["qout_ia"] for row in perfil_weekend]
    profile_qout_ia = [row["qout_ia"] for row in perfil]
    qout_ia_samples = [(idx, float(value)) for idx, value in enumerate(profile_qout_ia) if value is not None]
    chart_qout_vertical_lines = []
    if qout_ia_samples:
        max_idx, _ = max(qout_ia_samples, key=lambda item: item[1])
        min_idx, _ = min(qout_ia_samples, key=lambda item: item[1])
        chart_qout_vertical_lines.append({"index": max_idx, "color": "#c62828", "dash": "6 4"})
        chart_qout_vertical_lines.append({"index": min_idx, "color": "#616161", "dash": "6 4"})
    yesterday_rows = get_sqlite_day_medidas(cfg.nombre, yesterday)
    prev_day_qout_map = {
        slot_index_from_datetime(dt): (float(qout) if qout is not None else None)
        for dt, _, _, qout in yesterday_rows
    }
    prev_day_qout_real = [prev_day_qout_map.get(slot) for slot in range(96)]
    hist_labels = [dt.strftime("%H:%M") for dt, _, _, _ in recent]
    hist_qin = [qin for _, qin, _, _ in recent]
    hist_qout = [qout for _, _, _, qout in recent]

    return render_template_string(
        IA_TEMPLATE,
        nombre=cfg.nombre,
        sqlite_path=sqlite_path,
        perfil_disponible=bool(perfil),
        perfil_tipo_label=(perfil[0]["profile_label"] if perfil else profile_group_label(selected_profile_group)),
        selected_profile_group=selected_profile_group,
        default_profile_group_label=profile_group_label(default_profile_group),
        meta=meta,
        last_fecha=last_fecha,
        last_qin=last_qin,
        last_qin_fecha=last_qin_fecha,
        last_vol=last_vol,
        max_qin_historico=max_qin_historico,
        max_vol_ideal=max_vol_ideal,
        rebalse_card=rebalse_card,
        bajo_10_card=bajo_10_card,
        chart_vol_max_y=chart_vol_max_y,
        volumen_banda_min=volumen_banda_min,
        volumen_banda_max=volumen_banda_max,
        target_vol=target_vol,
        qin_fijo=qin_fijo,
        qin_requerido=qin_ideal_sugerido,
        qin_requerido_base=qin_requerido,
        target_vol_input=target_vol_input,
        qout_avg_24h=_series_mean("qout_promedio"),
        qout_ia_24h=_series_mean("qout_ia"),
        profile_labels=profile_labels,
        profile_qout_weekday_ia=profile_qout_weekday_ia,
        profile_qout_weekend_ia=profile_qout_weekend_ia,
        profile_qout_ia=profile_qout_ia,
        chart_qout_vertical_lines=chart_qout_vertical_lines,
        prev_day_qout_real=prev_day_qout_real,
        projection_labels=projection_labels,
        projection_qout_avg=projection_qout_avg,
        projection_qout_ia=projection_qout_ia,
        projection_vol_avg=projection_vol_avg,
        projection_vol_ia=projection_vol_ia,
        day_profile_labels=day_profile_labels,
        day_profile_tooltip_dates=day_profile_tooltip_dates,
        day_projection_vol_avg=day_projection_vol_avg,
        day_projection_vol_ia=day_projection_vol_ia,
        day_projection_vol_ideal_ia=day_projection_vol_ideal_ia,
        real_today_vol=real_today_vol,
        current_chile_slot_float=current_chile_slot_float,
        chart_vol_vertical_lines=chart_vol_vertical_lines,
        vol_view=vol_view,
        projection_table=projection_table,
        hist_labels=hist_labels,
        hist_qin=hist_qin,
        hist_qout=hist_qout,
    )


@app.route("/recintos/<nombre>/append", methods=["POST"])
def append_recinto(nombre: str):
    """
    Botón en la tabla: modo APPEND con contexto desde la última fecha no-vacía
    del SQLite hasta ahora, para rellenar bins vacíos al final.
    """
    paso_min = int(request.form.get("paso_min") or 15)
    result = run_append_for_recinto(nombre.strip(), paso_min=paso_min, source_label="botón fila")
    return redirect(url_for("index", status_msg=str(result.get("status_msg") or "")))


@app.route("/recintos/<nombre>/limpiar_sqlite", methods=["POST"])
def limpiar_recinto_sqlite(nombre: str):
    """
    FULL: Borra toda la info (tabla `medidas`) del SQLite de un recinto
    y vuelve a calcular/atraer desde MySQL legacy para el rango enviado
    (t_ini, t_fin, paso_min).
    """
    nombre_actual = nombre.strip()
    cfg = load_recinto_config(nombre_actual)
    if not cfg:
        return redirect(url_for("index", status_msg=f"Recinto {nombre_actual} no encontrado."))

    paso_min = int(request.form.get("paso_min") or 15)
    t_ini_str = request.form.get("t_ini")
    t_fin_str = request.form.get("t_fin")

    # Defaults si no vienen desde el botón
    t_ini = datetime(2026, 1, 1, 0, 0)
    t_fin = datetime.now().replace(second=0, microsecond=0)
    if t_ini_str:
        t_ini = _parse_dt_local(t_ini_str)
    if t_fin_str:
        t_fin = _parse_dt_local(t_fin_str)

    # 1) probar conexión a la fuente seleccionada antes de borrar/ejecutar
    if cfg.source_type == "mysql_legacy":
        try:
            conn_test = get_mysql_connection()
            conn_test.close()
            conn_ok = True
        except Exception as exc:
            conn_ok = False
            err_msg = f"Error conectando a MySQL legacy: {exc}"
    else:
        conn_ok = True
        err_msg = ""

    if not conn_ok:
        return redirect(url_for("index", status_msg=err_msg))

    # 2) borrar y recalcular
    clear_recinto_medidas(nombre_actual)
    series = calcular_series_recinto(cfg, t_ini, t_fin, paso_min)
    total_bins = len(series)
    n_rows = insert_medidas_batch(nombre_actual, series)

    cfg.last_run_at = datetime.now().isoformat(timespec="minutes")
    cfg.last_rows_saved = n_rows
    save_recinto_config(cfg)

    status_msg = (
        f"FULL: Conexión {cfg.source_type} OK. Recalculando {total_bins} intervalos de {paso_min} min "
        f"desde {t_ini.isoformat(timespec='minutes')} hasta {t_fin.isoformat(timespec='minutes')}. "
        f"Fin de proceso, {n_rows} registros guardados/actualizados en SQLite para recinto {nombre_actual}."
    )
    return redirect(url_for("index", status_msg=status_msg))


@app.route("/recintos/<nombre>/renombrar", methods=["POST"])
def renombrar_recinto(nombre: str):
    """
    Renombra un recinto:
    - actualiza la clave primaria `nombre` en `rt3_ia_config.sqlite`
    - renombra el SQLite del recinto (<nombre>.sqlite) si existe
    """
    nombre_actual = nombre.strip()
    nuevo_nombre = request.form.get("nuevo_nombre", "").strip()
    if not nuevo_nombre:
        return redirect(url_for("ver_recinto", nombre=nombre_actual))

    if nuevo_nombre == nombre_actual:
        return redirect(url_for("ver_recinto", nombre=nombre_actual))

    # Verificar que el recinto actual exista
    cfg_actual = load_recinto_config(nombre_actual)
    if not cfg_actual:
        return redirect(url_for("index", status_msg=f"Recinto {nombre_actual} no encontrado."))

    # Verificar que el nuevo nombre no exista
    if load_recinto_config(nuevo_nombre):
        return redirect(
            url_for(
                "index",
                status_msg=f"Ya existe un recinto con nombre '{nuevo_nombre}'.",
            )
        )

    # Renombrar SQLite primero (si existe)
    old_db = get_recinto_db_path(nombre_actual)
    new_db = get_recinto_db_path(nuevo_nombre)
    try:
        if old_db.exists():
            if new_db.exists() and new_db.resolve() != old_db.resolve():
                return redirect(
                    url_for("index", status_msg=f"Ya existe el SQLite para '{nuevo_nombre}'.")
                )
            os.rename(old_db, new_db)
    except Exception as exc:
        return redirect(url_for("index", status_msg=f"Error renombrando SQLite: {exc}"))

    # Actualizar nombre en sqlite config
    init_config_db()
    conn = sqlite3.connect(RT3_IA_CONFIG_DB)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE recintos SET nombre = ? WHERE nombre = ?", (nuevo_nombre, nombre_actual))
        conn.commit()
    finally:
        conn.close()

    return redirect(url_for("ver_recinto", nombre=nuevo_nombre))


@app.route("/recintos/<nombre>/config", methods=["GET", "POST"])
def editar_recinto_config(nombre: str):
    """
    Página para editar la configuración (tags, unidades, activo) de un recinto.
    """
    nombre_actual = nombre.strip()
    cfg = load_recinto_config(nombre_actual)
    if not cfg:
        return redirect(url_for("index", status_msg=f"Recinto {nombre_actual} no encontrado."))

    if request.method == "POST":
        tag_qin1, point_qin1, source_qin1 = infer_source_from_field(request.form.get("point_qin1"))
        tag_qin2, point_qin2, source_qin2 = infer_source_from_field(request.form.get("point_qin2"))
        tag_vol, point_vol, source_vol = infer_source_from_field(request.form.get("point_vol"))
        tag_qout, point_qout, source_qout = infer_source_from_field(request.form.get("point_qout"))
        source_candidates = [source_qin1, source_qin2, source_vol, source_qout]
        source_type = "influxdb" if "influxdb" in source_candidates else "mysql_legacy"
        volumen_maximo = safe_float(request.form.get("volumen_maximo"))
        qout_unit = normalize_qout_unit(request.form.get("qout_unit"))
        activo = request.form.get("activo") is not None

        cfg_new = RecintoConfig(
            nombre=cfg.nombre,
            tag_qin1=tag_qin1,
            tag_qin2=tag_qin2,
            tag_vol=tag_vol,
            tag_qout=tag_qout,
            point_qin1=point_qin1,
            point_qin2=point_qin2,
            point_vol=point_vol,
            point_qout=point_qout,
            source_type=source_type,
            volumen_maximo=volumen_maximo,
            qout_unit=qout_unit,
            activo=activo,
            last_run_at=cfg.last_run_at,
            last_rows_saved=cfg.last_rows_saved,
        )
        save_recinto_config(cfg_new)
        return redirect(url_for("index", status_msg=f"Configuración guardada para {cfg_new.nombre}."))

    return render_template_string(
        CONFIG_TEMPLATE,
        nombre=cfg.nombre,
        point_qin1=cfg.point_qin1,
        point_qin2=cfg.point_qin2,
        point_vol=cfg.point_vol,
        point_qout=cfg.point_qout,
        source_type=cfg.source_type,
        volumen_maximo=cfg.volumen_maximo,
        qout_unit=cfg.qout_unit,
        activo=cfg.activo,
    )


@app.route("/recintos/<nombre>/eliminar", methods=["POST"])
def eliminar_recinto(nombre: str):
    """
    Elimina un recinto desde la tabla HTML.
    """
    delete_recinto(nombre.strip())
    return redirect(url_for("index"))


@app.route("/recintos/<nombre>/procesar", methods=["POST"])
def reprocesar_recinto(nombre: str):
    """
    Reprocesa un recinto ya configurado.
    JSON:
      { "t_ini": "YYYY-MM-DDTHH:MM", "t_fin": "YYYY-MM-DDTHH:MM", "paso_min": 15 }
    """
    cfg = load_recinto_config(nombre.strip())
    if not cfg:
        return jsonify({"error": "recinto no encontrado"}), 404

    if not request.is_json:
        return jsonify({"error": "se requiere JSON"}), 400

    data = request.get_json(force=True)
    t_ini_str = data.get("t_ini")
    t_fin_str = data.get("t_fin")
    paso_min = int(data.get("paso_min") or 15)

    if not t_ini_str or not t_fin_str:
        return jsonify({"error": "t_ini y t_fin son obligatorios"}), 400

    t_ini = _parse_dt_local(t_ini_str.replace(" ", "T")) if "T" not in t_ini_str else _parse_dt_local(t_ini_str)
    t_fin = _parse_dt_local(t_fin_str.replace(" ", "T")) if "T" not in t_fin_str else _parse_dt_local(t_fin_str)

    # probar conexión a la fuente seleccionada
    if cfg.source_type == "mysql_legacy":
        try:
            conn_test = get_mysql_connection()
            conn_test.close()
            conn_ok = True
        except Exception as exc:
            conn_ok = False
            err_msg = f"Error conectando a MySQL legacy: {exc}"
    else:
        conn_ok = True
        err_msg = ""

    if not conn_ok:
        return jsonify({"status": "error", "message": err_msg}), 500

    series = calcular_series_recinto(cfg, t_ini, t_fin, paso_min)
    total_bins = len(series)
    n_rows = insert_medidas_batch(cfg.nombre, series)

    # actualizar info de última migración
    cfg.last_run_at = datetime.now().isoformat(timespec="minutes")
    cfg.last_rows_saved = n_rows
    save_recinto_config(cfg)

    return jsonify(
        {
            "status": "ok",
            "recinto": cfg.nombre,
            "rows_saved": n_rows,
            "total_bins": total_bins,
            "message": (
                f"Conexión {cfg.source_type} OK. Procesando {total_bins} intervalos de {paso_min} min. "
                f"Fin de proceso, {n_rows} registros guardados en SQLite."
            ),
            "db_path": str(get_recinto_db_path(cfg.nombre)),
        }
    )


if __name__ == "__main__":
    # Para pruebas locales:
    port = int(os.environ.get("PORT", 5058))
    app.run(host="0.0.0.0", port=port, debug=True)
