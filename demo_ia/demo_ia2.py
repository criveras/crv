#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_ia2.py

Construye un perfil de consumo cada 15 minutos a partir de un SQLite de rt3-ia.

Entrada:
  --sqlite nombre.sqlite

Lee tabla:
  medidas(fecha, qin, vol, qout)

Genera tabla:
  perfil_consumo_15min(
      slot_index INTEGER PRIMARY KEY,
      hora_texto TEXT,
      qout_promedio REAL,
      qout_ia REAL
  )
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

try:
    from xgboost import XGBRegressor  # type: ignore
    MODEL_KIND = "xgboost"
except Exception:
    XGBRegressor = None
    from sklearn.ensemble import GradientBoostingRegressor
    MODEL_KIND = "gradient_boosting"


START_DATE = datetime(2026, 1, 1, 0, 0)
SLOT_COUNT = 96
MAX_GAP_MINUTES = 60
PROFILE_TYPES = {
    "weekday": {"label": "Lunes a viernes", "dows": {0, 1, 2, 3, 4}, "forecast_dow": 0},
    "saturday": {"label": "Sabado", "dows": {5}, "forecast_dow": 5},
    "sunday": {"label": "Domingo", "dows": {6}, "forecast_dow": 6},
}


@dataclass
class Medida:
    timestamp: datetime
    qout: float


@dataclass
class FeatureRow:
    timestamp: datetime
    slot_index: int
    dow: int
    qout: float
    qout_lag1: Optional[float]
    qout_lag4: Optional[float]
    qout_lag96: Optional[float]
    rolling_mean_4: Optional[float]
    rolling_mean_16: Optional[float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Perfil IA de consumo cada 15 minutos")
    parser.add_argument("--sqlite", required=True, help="Ruta al archivo SQLite de rt3-ia")
    parser.add_argument(
        "--table",
        default="perfil_consumo_15min",
        help="Tabla de salida en SQLite (default: perfil_consumo_15min)",
    )
    return parser.parse_args()


def open_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def load_medidas(conn: sqlite3.Connection) -> List[Medida]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT fecha AS timestamp, qout
        FROM medidas
        WHERE fecha >= ?
        ORDER BY fecha ASC
        """,
        (START_DATE.isoformat(timespec="minutes"),),
    )
    out: List[Medida] = []
    for row in cur.fetchall():
        ts_raw = row["timestamp"]
        qout_raw = row["qout"]
        if ts_raw is None or qout_raw is None:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw))
            qout = float(qout_raw)
        except Exception:
            continue
        if qout < 0:
            continue
        out.append(Medida(timestamp=ts, qout=qout))
    out.sort(key=lambda x: x.timestamp)
    return out


def drop_large_gaps(rows: Sequence[Medida], max_gap_minutes: int = MAX_GAP_MINUTES) -> List[Medida]:
    if not rows:
        return []
    filtered: List[Medida] = [rows[0]]
    max_gap = timedelta(minutes=max_gap_minutes)
    for row in rows[1:]:
        prev = filtered[-1]
        gap = row.timestamp - prev.timestamp
        if gap <= max_gap:
            filtered.append(row)
    return filtered


def compute_slot_index(ts: datetime) -> int:
    return ts.hour * 4 + ts.minute // 15


def mean_or_none(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return float(sum(values) / len(values))


def build_feature_rows(rows: Sequence[Medida]) -> List[FeatureRow]:
    features: List[FeatureRow] = []
    qouts = [row.qout for row in rows]
    for idx, row in enumerate(rows):
        lag1 = qouts[idx - 1] if idx >= 1 else None
        lag4 = qouts[idx - 4] if idx >= 4 else None
        lag96 = qouts[idx - 96] if idx >= 96 else None
        rolling4 = mean_or_none(qouts[max(0, idx - 4):idx]) if idx >= 1 else None
        rolling16 = mean_or_none(qouts[max(0, idx - 16):idx]) if idx >= 1 else None
        features.append(
            FeatureRow(
                timestamp=row.timestamp,
                slot_index=compute_slot_index(row.timestamp),
                dow=row.timestamp.weekday(),
                qout=row.qout,
                qout_lag1=lag1,
                qout_lag4=lag4,
                qout_lag96=lag96,
                rolling_mean_4=rolling4,
                rolling_mean_16=rolling16,
            )
        )
    return features


def to_training_matrix(rows: Sequence[FeatureRow]) -> Tuple[np.ndarray, np.ndarray]:
    x_rows: List[List[float]] = []
    y_rows: List[float] = []
    for row in rows:
        values = [
            row.slot_index,
            row.dow,
            row.qout_lag1,
            row.qout_lag4,
            row.qout_lag96,
            row.rolling_mean_4,
            row.rolling_mean_16,
        ]
        if any(v is None or not math.isfinite(float(v)) for v in values):
            continue
        x_rows.append([float(v) for v in values])
        y_rows.append(float(row.qout))
    if not x_rows:
        raise ValueError("No hay suficientes filas para entrenar el modelo después de aplicar lags.")
    return np.asarray(x_rows, dtype=float), np.asarray(y_rows, dtype=float)


def build_model():
    if MODEL_KIND == "xgboost" and XGBRegressor is not None:
        return XGBRegressor(
            n_estimators=300,
            max_depth=6,
            learning_rate=0.05,
            subsample=0.8,
            objective="reg:squarederror",
            random_state=42,
        )
    return GradientBoostingRegressor(
        n_estimators=300,
        max_depth=6,
        learning_rate=0.05,
        subsample=0.8,
        random_state=42,
    )


def compute_historical_average(rows: Sequence[Medida]) -> List[Optional[float]]:
    buckets: Dict[int, List[float]] = {slot: [] for slot in range(SLOT_COUNT)}
    for row in rows:
        buckets[compute_slot_index(row.timestamp)].append(float(row.qout))
    return [mean_or_none(buckets[slot]) for slot in range(SLOT_COUNT)]


def profile_type_for_dow(dow: int) -> str:
    if dow == 5:
        return "saturday"
    if dow == 6:
        return "sunday"
    return "weekday"


def filter_rows_by_profile_type(rows: Sequence[Medida], profile_type: str) -> List[Medida]:
    cfg = PROFILE_TYPES[profile_type]
    allowed = cfg["dows"]
    return [row for row in rows if row.timestamp.weekday() in allowed]


def clamp_prediction(value: float, max_hist: float) -> float:
    if not math.isfinite(value):
        return 0.0
    upper = max_hist * 1.5 if max_hist > 0 else 0.0
    return max(0.0, min(float(value), upper))


def rolling_mean(history: Sequence[float], size: int) -> float:
    if not history:
        return 0.0
    window = history[-size:]
    return float(sum(window) / len(window))


def recursive_forecast(
    model,
    rows: Sequence[Medida],
    forecast_dow: Optional[int] = None,
    steps: int = SLOT_COUNT,
) -> List[Tuple[int, float]]:
    if not rows:
        raise ValueError("No hay histórico disponible para forecast.")
    max_hist = max(float(r.qout) for r in rows)
    last_ts = rows[-1].timestamp
    history = [float(r.qout) for r in rows]
    future_raw: List[Tuple[int, float]] = []
    for step in range(1, steps + 1):
        ts = last_ts + timedelta(minutes=15 * step)
        slot_index = compute_slot_index(ts)
        dow = int(forecast_dow) if forecast_dow is not None else ts.weekday()
        lag1 = history[-1] if len(history) >= 1 else 0.0
        lag4 = history[-4] if len(history) >= 4 else lag1
        lag96 = history[-96] if len(history) >= 96 else lag1
        rm4 = rolling_mean(history, 4)
        rm16 = rolling_mean(history, 16)
        x = np.asarray([[slot_index, dow, lag1, lag4, lag96, rm4, rm16]], dtype=float)
        pred = float(model.predict(x)[0])
        pred = clamp_prediction(pred, max_hist)
        history.append(pred)
        future_raw.append((slot_index, pred))

    smoothed = smooth_series([pred for _, pred in future_raw])
    return [(future_raw[idx][0], smoothed[idx]) for idx in range(len(future_raw))]


def smooth_series(values: Sequence[float]) -> List[float]:
    if not values:
        return []
    out: List[float] = []
    for idx, value in enumerate(values):
        lo = max(0, idx - 1)
        hi = min(len(values), idx + 2)
        out.append(float(sum(values[lo:hi]) / (hi - lo)))
    return out


def ensure_output_table(conn: sqlite3.Connection, table_name: str) -> None:
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            profile_type TEXT NOT NULL DEFAULT 'weekday',
            profile_label TEXT NOT NULL DEFAULT 'Lunes a viernes',
            slot_index INTEGER NOT NULL,
            hora_texto TEXT NOT NULL,
            qout_promedio REAL,
            qout_ia REAL,
            PRIMARY KEY (profile_type, slot_index)
        )
        """
    )
    cur.execute(f"PRAGMA table_info({table_name})")
    cols = [row[1] for row in cur.fetchall()]
    if "profile_type" not in cols:
        cur.execute(f"ALTER TABLE {table_name} RENAME TO {table_name}_legacy")
        cur.execute(
            f"""
            CREATE TABLE {table_name} (
                profile_type TEXT NOT NULL DEFAULT 'weekday',
                profile_label TEXT NOT NULL DEFAULT 'Lunes a viernes',
                slot_index INTEGER NOT NULL,
                hora_texto TEXT NOT NULL,
                qout_promedio REAL,
                qout_ia REAL,
                PRIMARY KEY (profile_type, slot_index)
            )
            """
        )
        cur.execute(
            f"""
            INSERT INTO {table_name}(profile_type, profile_label, slot_index, hora_texto, qout_promedio, qout_ia)
            SELECT 'weekday', 'Lunes a viernes', slot_index, hora_texto, qout_promedio, qout_ia
            FROM {table_name}_legacy
            """
        )
        cur.execute(f"DROP TABLE {table_name}_legacy")
    conn.commit()


def ensure_metadata_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS perfil_consumo_15min_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()


def save_metadata(conn: sqlite3.Connection, metadata: Dict[str, object]) -> None:
    ensure_metadata_table(conn)
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO perfil_consumo_15min_meta(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        [(str(k), json.dumps(v, ensure_ascii=False)) for k, v in metadata.items()],
    )
    conn.commit()


def model_pickle_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.stem + "_perfil_consumo_15min.pkl")


def save_model_pickle(model, db_path: Path) -> Path:
    pkl_path = model_pickle_path(db_path)
    with open(pkl_path, "wb") as fh:
        pickle.dump(model, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return pkl_path


def save_profile(
    conn: sqlite3.Connection,
    table_name: str,
    profiles: Dict[str, Dict[str, object]],
) -> None:
    ensure_output_table(conn, table_name)
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {table_name}")
    rows = []
    for profile_type, payload in profiles.items():
        profile_label = str(payload["profile_label"])
        qout_avg = payload["qout_avg"]
        qout_ia_by_slot = payload["qout_ia_by_slot"]
        for slot in range(SLOT_COUNT):
            hour = slot // 4
            minute = (slot % 4) * 15
            hora_texto = f"{hour:02d}:{minute:02d}"
            rows.append((profile_type, profile_label, slot, hora_texto, qout_avg[slot], qout_ia_by_slot.get(slot)))
    cur.executemany(
        f"""
        INSERT INTO {table_name} (profile_type, profile_label, slot_index, hora_texto, qout_promedio, qout_ia)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def main() -> int:
    args = parse_args()
    db_path = Path(args.sqlite).expanduser()
    if not db_path.is_absolute():
        db_path = (Path.cwd() / db_path).resolve()
    if not db_path.exists():
        raise SystemExit(f"No existe SQLite: {db_path}")

    conn = open_sqlite(db_path)
    try:
        medidas = load_medidas(conn)
        medidas = drop_large_gaps(medidas)
        if not medidas:
            raise SystemExit("No hay datos válidos en la tabla medidas.")

        features = build_feature_rows(medidas)
        x_train, y_train = to_training_matrix(features)
        model = build_model()
        model.fit(x_train, y_train)

        profile_payloads: Dict[str, Dict[str, object]] = {}
        for profile_type, cfg in PROFILE_TYPES.items():
            filtered = filter_rows_by_profile_type(medidas, profile_type)
            source_rows = filtered if filtered else medidas
            qout_avg = compute_historical_average(source_rows)
            qout_ia_forecast = recursive_forecast(
                model,
                medidas,
                forecast_dow=int(cfg["forecast_dow"]),
                steps=SLOT_COUNT,
            )
            qout_ia_by_slot = {slot_index: pred for slot_index, pred in qout_ia_forecast}
            profile_payloads[profile_type] = {
                "profile_label": cfg["label"],
                "qout_avg": qout_avg,
                "qout_ia_by_slot": qout_ia_by_slot,
                "history_rows": len(filtered),
            }
        save_profile(conn, args.table, profile_payloads)
        pkl_path = save_model_pickle(model, db_path)
        metadata = {
            "trained_at": datetime.now().isoformat(timespec="seconds"),
            "model_kind": MODEL_KIND,
            "sqlite_path": str(db_path),
            "model_pkl_path": str(pkl_path),
            "source_table": "medidas",
            "target_table": args.table,
            "history_rows_valid": len(medidas),
            "training_rows": int(len(x_train)),
            "history_start": medidas[0].timestamp.isoformat(timespec="minutes"),
            "history_end": medidas[-1].timestamp.isoformat(timespec="minutes"),
            "forecast_start": (medidas[-1].timestamp + timedelta(minutes=15)).isoformat(timespec="minutes"),
            "forecast_end": (medidas[-1].timestamp + timedelta(minutes=15 * SLOT_COUNT)).isoformat(timespec="minutes"),
            "slot_count": SLOT_COUNT,
            "profile_types": {
                profile_type: {
                    "label": payload["profile_label"],
                    "history_rows": payload["history_rows"],
                }
                for profile_type, payload in profile_payloads.items()
            },
            "max_gap_minutes": MAX_GAP_MINUTES,
            "model_params": {
                "n_estimators": 300,
                "max_depth": 6,
                "learning_rate": 0.05,
                "subsample": 0.8,
            },
            "features": [
                "slot_index",
                "dow",
                "qout_lag1",
                "qout_lag4",
                "qout_lag96",
                "rolling_mean_4",
                "rolling_mean_16",
            ],
        }
        save_metadata(conn, metadata)

        print("Perfil generado OK")
        print(f"SQLite: {db_path}")
        print(f"Modelo: {MODEL_KIND}")
        print(f"Historico valido: {len(medidas)} registros")
        print(f"Training rows: {len(x_train)}")
        print(f"Tabla destino: {args.table}")
        print(f"Modelo PKL: {pkl_path}")
        print("Metadata SQLite: perfil_consumo_15min_meta")
        print("Primeros 5 slots:")
        cur = conn.cursor()
        for row in cur.execute(
            f"SELECT profile_type, slot_index, hora_texto, qout_promedio, qout_ia FROM {args.table} ORDER BY profile_type, slot_index LIMIT 9"
        ):
            print(dict(row))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
