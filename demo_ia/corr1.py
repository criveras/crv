#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
corr1.py

Analisis de correlacion + proyeccion de series temporales multivariables
para datos de correlacion en SQLite (tabla: correlation_data).

Requisitos principales:
  pip install torch pandas numpy

Uso ejemplo:
  python3 corr1.py \
    --sqlite /home/criveras/app/rt3-ia/corr_mi_proyecto.sqlite \
    --target TAG_OBJETIVO \
    --model gru \
    --window 24 \
    --horizon 12 \
    --epochs 30 \
    --outdir /home/criveras/app/rt3-ia/out_corr1
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset


def set_seed(seed: int) -> None:
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def load_correlation_dataframe(sqlite_path: Path) -> pd.DataFrame:
    if not sqlite_path.exists():
        raise FileNotFoundError(f"No existe SQLite: {sqlite_path}")
    conn = sqlite3.connect(sqlite_path)
    try:
        df = pd.read_sql_query("SELECT * FROM correlation_data ORDER BY fecha ASC", conn)
    finally:
        conn.close()
    if df.empty:
        raise ValueError("Tabla correlation_data vacia")
    if "fecha" not in df.columns:
        raise ValueError("No existe columna fecha en correlation_data")
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    df = df.dropna(subset=["fecha"]).sort_values("fecha").reset_index(drop=True)
    # Convertir columnas de tags a numericas e interpolar vacios
    for c in df.columns:
        if c == "fecha":
            continue
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.set_index("fecha")
    df = df.interpolate(method="time").ffill().bfill()
    return df


def save_correlation_outputs(df: pd.DataFrame, outdir: Path) -> pd.DataFrame:
    outdir.mkdir(parents=True, exist_ok=True)
    corr = df.corr(method="pearson")
    corr.to_csv(outdir / "correlation_matrix.csv", index=True)
    return corr


def normalize_target_name(name: str) -> str:
    return " ".join(str(name).strip().lower().split())


def resolve_target_column(requested_target: str, columns: list[str]) -> str:
    if requested_target in columns:
        return requested_target

    normalized_map = {normalize_target_name(col): col for col in columns}
    normalized_target = normalize_target_name(requested_target)
    if normalized_target in normalized_map:
        return normalized_map[normalized_target]

    close_matches = difflib.get_close_matches(requested_target, columns, n=5, cutoff=0.5)
    normalized_matches = difflib.get_close_matches(normalized_target, list(normalized_map.keys()), n=5, cutoff=0.5)
    suggested = []
    for item in close_matches + [normalized_map[m] for m in normalized_matches]:
        if item not in suggested:
            suggested.append(item)

    placeholder_targets = {"tag_objetivo", "codigo_tag_objetivo"}
    details = []
    if normalized_target in placeholder_targets:
        details.append(
            "El valor indicado parece ser un marcador del ejemplo de uso, no una columna real de la tabla."
        )
    if suggested:
        details.append(f"Sugerencias: {suggested}")
    details.append(f"Columnas disponibles: {columns}")
    raise ValueError(f"Target '{requested_target}' no existe. " + " ".join(details))


@dataclass
class SplitData:
    x_train: np.ndarray
    y_train: np.ndarray
    x_val: np.ndarray
    y_val: np.ndarray
    x_test: np.ndarray
    y_test: np.ndarray
    train_mean: np.ndarray
    train_std: np.ndarray


def build_sequences(
    data: np.ndarray,
    target_idx: int,
    window: int,
) -> Tuple[np.ndarray, np.ndarray]:
    xs, ys = [], []
    n = len(data)
    if n <= window:
        raise ValueError("No hay suficientes filas para construir secuencias")
    for i in range(window, n):
        xs.append(data[i - window : i, :])
        ys.append(data[i, target_idx])
    return np.array(xs, dtype=np.float32), np.array(ys, dtype=np.float32).reshape(-1, 1)


def split_and_scale(features: np.ndarray, target_idx: int, window: int) -> SplitData:
    n = len(features)
    n_train = max(1, int(n * 0.7))
    n_val = max(1, int(n * 0.15))
    n_test = n - n_train - n_val
    if n_test < 1:
        n_test = 1
        n_train = max(1, n_train - 1)

    train = features[:n_train]
    val = features[n_train : n_train + n_val]
    test = features[n_train + n_val :]

    train_mean = train.mean(axis=0)
    train_std = train.std(axis=0)
    train_std = np.where(train_std < 1e-8, 1.0, train_std)

    train_n = (train - train_mean) / train_std
    val_n = (val - train_mean) / train_std
    test_n = (test - train_mean) / train_std

    x_train, y_train = build_sequences(train_n, target_idx, window)
    x_val, y_val = build_sequences(val_n, target_idx, window) if len(val_n) > window else (x_train[:1], y_train[:1])
    x_test, y_test = build_sequences(test_n, target_idx, window) if len(test_n) > window else (x_val[:1], y_val[:1])

    return SplitData(
        x_train=x_train,
        y_train=y_train,
        x_val=x_val,
        y_val=y_val,
        x_test=x_test,
        y_test=y_test,
        train_mean=train_mean,
        train_std=train_std,
    )


class SeqDataset(Dataset):
    def __init__(self, x: np.ndarray, y: np.ndarray):
        self.x = torch.from_numpy(x)
        self.y = torch.from_numpy(y)

    def __len__(self) -> int:
        return len(self.x)

    def __getitem__(self, idx: int):
        return self.x[idx], self.y[idx]


class RnnForecaster(nn.Module):
    def __init__(self, input_size: int, hidden_size: int, model_kind: str = "gru", num_layers: int = 1):
        super().__init__()
        model_kind = model_kind.lower().strip()
        if model_kind == "lstm":
            self.rnn = nn.LSTM(input_size, hidden_size, num_layers=num_layers, batch_first=True)
        else:
            self.rnn = nn.GRU(input_size, hidden_size, num_layers=num_layers, batch_first=True)
        self.head = nn.Sequential(
            nn.Linear(hidden_size, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, 1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out, _ = self.rnn(x)
        last = out[:, -1, :]
        return self.head(last)


class PositionalEncoding(nn.Module):
    def __init__(self, d_model: int, max_len: int = 4096):
        super().__init__()
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float32).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2, dtype=torch.float32) * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        self.register_buffer("pe", pe.unsqueeze(0), persistent=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.pe[:, : x.size(1), :]


class TransformerForecaster(nn.Module):
    def __init__(
        self,
        input_size: int,
        hidden_size: int,
        num_layers: int = 2,
        num_heads: int = 4,
        dropout: float = 0.1,
    ):
        super().__init__()
        if hidden_size % num_heads != 0:
            raise ValueError("hidden_size debe ser divisible por num_heads para transformer")
        self.input_proj = nn.Linear(input_size, hidden_size)
        self.pos_encoder = PositionalEncoding(hidden_size)
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=hidden_size,
            nhead=num_heads,
            dim_feedforward=hidden_size * 4,
            dropout=dropout,
            batch_first=True,
            norm_first=True,
            activation="gelu",
        )
        self.encoder = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)
        self.norm = nn.LayerNorm(hidden_size)
        self.head = nn.Sequential(
            nn.Linear(hidden_size, hidden_size),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size, 1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.input_proj(x)
        x = self.pos_encoder(x)
        out = self.encoder(x)
        last = self.norm(out[:, -1, :])
        return self.head(last)


def evaluate(model: nn.Module, loader: DataLoader, device: torch.device) -> Tuple[float, float]:
    model.eval()
    mse_sum = 0.0
    mae_sum = 0.0
    n = 0
    with torch.no_grad():
        for xb, yb in loader:
            xb = xb.to(device)
            yb = yb.to(device)
            pred = model(xb)
            mse = torch.mean((pred - yb) ** 2).item()
            mae = torch.mean(torch.abs(pred - yb)).item()
            bs = xb.shape[0]
            mse_sum += mse * bs
            mae_sum += mae * bs
            n += bs
    return mse_sum / max(1, n), mae_sum / max(1, n)


def train_model(
    split: SplitData,
    input_size: int,
    model_kind: str,
    hidden_size: int,
    num_layers: int,
    num_heads: int,
    dropout: float,
    epochs: int,
    batch_size: int,
    lr: float,
    device: torch.device,
) -> Tuple[nn.Module, dict]:
    if model_kind == "transformer":
        model = TransformerForecaster(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            num_heads=num_heads,
            dropout=dropout,
        ).to(device)
    else:
        model = RnnForecaster(
            input_size=input_size,
            hidden_size=hidden_size,
            model_kind=model_kind,
            num_layers=num_layers,
        ).to(device)
    loss_fn = nn.MSELoss()
    opt = torch.optim.Adam(model.parameters(), lr=lr)

    train_loader = DataLoader(SeqDataset(split.x_train, split.y_train), batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(SeqDataset(split.x_val, split.y_val), batch_size=batch_size, shuffle=False)
    test_loader = DataLoader(SeqDataset(split.x_test, split.y_test), batch_size=batch_size, shuffle=False)

    best_val = math.inf
    best_state = None

    for ep in range(1, epochs + 1):
        model.train()
        for xb, yb in train_loader:
            xb = xb.to(device)
            yb = yb.to(device)
            opt.zero_grad(set_to_none=True)
            pred = model(xb)
            loss = loss_fn(pred, yb)
            loss.backward()
            opt.step()
        val_mse, _ = evaluate(model, val_loader, device)
        if val_mse < best_val:
            best_val = val_mse
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        if ep % 10 == 0 or ep == 1 or ep == epochs:
            print(f"[train] epoch={ep:03d} val_mse={val_mse:.6f}")

    if best_state is not None:
        model.load_state_dict(best_state)

    train_mse, train_mae = evaluate(model, train_loader, device)
    val_mse, val_mae = evaluate(model, val_loader, device)
    test_mse, test_mae = evaluate(model, test_loader, device)
    metrics = {
        "train_mse": train_mse,
        "train_mae": train_mae,
        "val_mse": val_mse,
        "val_mae": val_mae,
        "test_mse": test_mse,
        "test_mae": test_mae,
    }
    return model, metrics


def forecast_horizon(
    model: nn.Module,
    normalized_full: np.ndarray,
    target_idx: int,
    window: int,
    horizon: int,
    mean: np.ndarray,
    std: np.ndarray,
    device: torch.device,
) -> np.ndarray:
    seq = normalized_full[-window:, :].copy()
    preds = []
    model.eval()
    for _ in range(horizon):
        x = torch.from_numpy(seq.astype(np.float32)).unsqueeze(0).to(device)
        with torch.no_grad():
            y_hat = model(x).cpu().item()
        preds.append(y_hat)
        next_row = seq[-1, :].copy()
        next_row[target_idx] = y_hat
        seq = np.vstack([seq[1:], next_row])
    # desnormalizar target
    preds = np.array(preds, dtype=np.float32)
    return preds * std[target_idx] + mean[target_idx]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analisis correlacion + GRU/LSTM/Transformer sobre correlation_data")
    p.add_argument("--sqlite", required=True, help="Ruta SQLite del proyecto de correlacion")
    p.add_argument("--target", required=True, help="codigo_tag objetivo a predecir")
    p.add_argument("--model", default="gru", choices=["gru", "lstm", "transformer"], help="Modelo de prediccion")
    p.add_argument("--window", type=int, default=24, help="Ventana historica (filas)")
    p.add_argument("--horizon", type=int, default=12, help="Horizonte de proyeccion (filas)")
    p.add_argument("--hidden", type=int, default=64, help="Neuronas ocultas")
    p.add_argument("--layers", type=int, default=2, help="Capas recurrentes/transformer")
    p.add_argument("--heads", type=int, default=4, help="Cabezas de atencion para transformer")
    p.add_argument("--dropout", type=float, default=0.1, help="Dropout del modelo")
    p.add_argument("--epochs", type=int, default=30, help="Epocas de entrenamiento")
    p.add_argument("--batch", type=int, default=64, help="Batch size")
    p.add_argument("--lr", type=float, default=1e-3, help="Learning rate")
    p.add_argument("--seed", type=int, default=42, help="Seed")
    p.add_argument("--outdir", default="./out_corr1", help="Directorio de salida")
    p.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"], help="Dispositivo")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    set_seed(args.seed)

    sqlite_path = Path(args.sqlite)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    df = load_correlation_dataframe(sqlite_path)
    corr = save_correlation_outputs(df, outdir)
    target_column = resolve_target_column(args.target, list(df.columns))
    target_idx = list(df.columns).index(target_column)

    features = df.values.astype(np.float32)
    split = split_and_scale(features, target_idx=target_idx, window=args.window)

    device = (
        torch.device("cuda")
        if (args.device == "auto" and torch.cuda.is_available()) or args.device == "cuda"
        else torch.device("cpu")
    )
    print(f"[info] device={device} rows={len(df)} cols={len(df.columns)} target={target_column}")

    model, metrics = train_model(
        split=split,
        input_size=features.shape[1],
        model_kind=args.model,
        hidden_size=args.hidden,
        num_layers=args.layers,
        num_heads=args.heads,
        dropout=args.dropout,
        epochs=args.epochs,
        batch_size=args.batch,
        lr=args.lr,
        device=device,
    )

    normalized_full = (features - split.train_mean) / split.train_std
    pred_target = forecast_horizon(
        model=model,
        normalized_full=normalized_full,
        target_idx=target_idx,
        window=args.window,
        horizon=args.horizon,
        mean=split.train_mean,
        std=split.train_std,
        device=device,
    )

    step = pd.infer_freq(df.index)
    if step is None:
        step_minutes = 15
        step_delta = pd.Timedelta(minutes=step_minutes)
    else:
        step_delta = pd.Timedelta(step)
    last_ts = df.index[-1]
    future_idx = [last_ts + step_delta * (i + 1) for i in range(args.horizon)]
    forecast_df = pd.DataFrame({"fecha": future_idx, f"{target_column}_pred": pred_target})
    forecast_df.to_csv(outdir / "forecast.csv", index=False)

    torch.save(
        {
            "model_state": model.state_dict(),
            "model_kind": args.model,
            "target": target_column,
            "window": args.window,
            "layers": args.layers,
            "heads": args.heads,
            "dropout": args.dropout,
            "input_columns": list(df.columns),
            "mean": split.train_mean.tolist(),
            "std": split.train_std.tolist(),
        },
        outdir / "model.pt",
    )

    metrics_payload = {
        "sqlite": str(sqlite_path),
        "target": target_column,
        "model": args.model,
        "window": args.window,
        "horizon": args.horizon,
        "layers": args.layers,
        "heads": args.heads,
        "dropout": args.dropout,
        "metrics": metrics,
        "top_corr_with_target": corr[target_column].sort_values(ascending=False).head(10).to_dict(),
    }
    with open(outdir / "metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics_payload, f, indent=2, ensure_ascii=False)

    print(f"[ok] outputs en: {outdir}")
    print(f"[ok] correlation_matrix.csv, forecast.csv, metrics.json, model.pt")


if __name__ == "__main__":
    main()
