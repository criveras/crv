#!/usr/bin/env python3
"""Modelo XGBoost GPU para pre-alarma de rotura."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import classification_report, roc_auc_score

from features import feature_matrix


def _gpu_params() -> dict[str, Any]:
    device = os.environ.get("XGB_DEVICE", "cuda")
    return {
        "n_estimators": 300,
        "max_depth": 6,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "tree_method": "hist",
        "device": device,
        "eval_metric": "auc",
        "verbosity": 0,
    }


def train_classifier(
    df: pd.DataFrame,
    test_size: float = 0.2,
) -> tuple[xgb.XGBClassifier, dict[str, Any]]:
    X, cols = feature_matrix(df)
    y = df["pre_rupture"].astype(int).values

    valid = ~np.isnan(y)
    X, y = X[valid], y[valid]

    if y.sum() < 5:
        raise ValueError(
            f"Muy pocos eventos pre-rotura ({int(y.sum())}). "
            "Amplía el rango histórico o ajusta delta_umbral/lookahead."
        )

    split_idx = int(len(X) * (1 - test_size))
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]

    scale = float((len(y_train) - y_train.sum()) / max(y_train.sum(), 1))

    model = xgb.XGBClassifier(**_gpu_params(), scale_pos_weight=scale)
    model.fit(X_train, y_train)

    y_prob = model.predict_proba(X_test)[:, 1]
    y_pred = (y_prob >= 0.5).astype(int)

    metrics: dict[str, Any] = {
        "features": cols,
        "train_size": len(X_train),
        "test_size": len(X_test),
        "positives_train": int(y_train.sum()),
        "positives_test": int(y_test.sum()),
    }
    if len(np.unique(y_test)) > 1:
        metrics["roc_auc"] = round(float(roc_auc_score(y_test, y_prob)), 4)
    metrics["report"] = classification_report(y_test, y_pred, zero_division=0)

    importances = sorted(
        zip(cols, model.feature_importances_.tolist()),
        key=lambda x: x[1],
        reverse=True,
    )
    metrics["top_features"] = [
        {"feature": f, "importance": round(float(imp), 4)} for f, imp in importances[:10]
    ]

    return model, metrics


def score_current(model: xgb.XGBClassifier, df: pd.DataFrame, cols: list[str]) -> dict[str, Any]:
    """Probabilidad de pre-rotura en el último instante."""
    if df.empty:
        return {"prob": 0.0, "nivel": 0, "estado": "sin_datos"}

    row = df.iloc[-1]
    X = row[cols].replace([np.inf, -np.inf], np.nan).fillna(0).values.reshape(1, -1)
    prob = float(model.predict_proba(X)[0, 1])

    if prob >= 0.75:
        nivel, estado = 3, "alarma"
    elif prob >= 0.50:
        nivel, estado = 2, "pre_alarma"
    elif prob >= 0.30:
        nivel, estado = 1, "vigilancia"
    else:
        nivel, estado = 0, "ok"

    return {
        "prob": round(prob, 4),
        "nivel": nivel,
        "estado": estado,
        "ts": str(row.get("time_local", "")),
        "caudal": round(float(row["value"]), 3),
        "delta_4": round(float(row.get("delta_4", 0)), 3),
        "dev_p80": round(float(row.get("dev_p80", 0)), 3),
        "vs_night_min": round(float(row.get("vs_night_min", 0)), 3),
    }


def save_model(model: xgb.XGBClassifier, cols: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(path))
    path.with_suffix(".meta.json").write_text(
        json.dumps({"features": cols}, indent=2),
        encoding="utf-8",
    )


def load_model(path: Path) -> tuple[xgb.XGBClassifier, list[str]]:
    model = xgb.XGBClassifier()
    model.load_model(str(path))
    cols = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))["features"]
    return model, cols
