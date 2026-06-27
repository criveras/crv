#!/usr/bin/env python3
"""
Lee tags desde Redis, acumula 15 minutos, calcula mediana y guarda en DuckDB (all.ddb).

Modo recomendado:
  --all-tags --duckdb /path/all.ddb

Modo legacy (un sqlite por tag):
  --all-tags --outdir /path/data
"""

from __future__ import annotations

import argparse
import signal
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

MY2SQLITE_DIR = Path(__file__).resolve().parent / "my2sqlite"
if str(MY2SQLITE_DIR) not in sys.path:
    sys.path.insert(0, str(MY2SQLITE_DIR))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Acumula 15 minutos de valores desde Redis y guarda mediana"
    )
    p.add_argument("--tagid", type=int, help="ID del tag (modo 1 tag)")
    p.add_argument("--redis-host", default="ibm", help="Host Redis")
    p.add_argument("--redis-port", type=int, default=6666, help="Puerto Redis")
    p.add_argument("--sample-seconds", type=float, default=30.0)
    p.add_argument("--window-minutes", type=int, default=15)
    p.add_argument("--sqlite", help="SQLite destino (modo 1 tag)")
    p.add_argument("--table", help='Tabla SQLite (default: "tag_<tagid>")')
    p.add_argument("--once", action="store_true")
    p.add_argument("--all-tags", action="store_true")
    p.add_argument("--outdir", help="Directorio sqlite por tag (legacy)")
    p.add_argument(
        "--duckdb",
        help="Archivo DuckDB unificado (ej: simula/all.ddb)",
    )
    return p.parse_args()


def floor_to_minutes(dt: datetime, step_minutes: int) -> datetime:
    m = (dt.minute // step_minutes) * step_minutes
    return dt.replace(minute=m, second=0, microsecond=0)


def setup_sqlite(sqlite_path: Path, table_name: str) -> None:
    try:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    conn = sqlite3.connect(sqlite_path)
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS "{table_name}" (
                fecha DATETIME PRIMARY KEY,
                valor REAL NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def save_median_sqlite(
    sqlite_path: Path, table_name: str, window_fecha: datetime, median_value: float
) -> None:
    conn = sqlite3.connect(sqlite_path)
    try:
        cur = conn.cursor()
        cur.execute(
            f'INSERT OR REPLACE INTO "{table_name}" (fecha, valor) VALUES (?, ?)',
            (window_fecha.isoformat(timespec="minutes"), float(median_value)),
        )
        conn.commit()
    finally:
        conn.close()


def save_median_all(outdir: Path, tagid: int, window_fecha: datetime, median_value: float) -> None:
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    sqlite_path = outdir / f"tag_{int(tagid):05d}.sqlite"
    table_name = f"tag_{int(tagid)}"
    setup_sqlite(sqlite_path, table_name)
    save_median_sqlite(sqlite_path, table_name, window_fecha, median_value)


class GracefulExit:
    exiting = False


def _signal_handler(signum, frame):
    GracefulExit.exiting = True


def save_medians_duckdb(duck_path: Path, window_fecha: datetime, rows: list[tuple[int, float]]) -> int:
    from duckdb_store import connect, save_medians

    last_exc: Exception | None = None
    for attempt in range(6):
        con = None
        try:
            con = connect(duck_path)
            saved = save_medians(con, window_fecha, rows)
            con.close()
            return saved
        except Exception as exc:
            last_exc = exc
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
            if "lock" not in str(exc).lower() or attempt >= 5:
                raise
            time.sleep(min(2.0 * (attempt + 1), 10.0))
    if last_exc:
        raise last_exc
    return 0


def try_import_redis():
    try:
        import redis  # type: ignore

        return redis
    except ModuleNotFoundError:
        print("Falta dependencia 'redis'. Instala con: pip install redis", file=sys.stderr)
        sys.exit(2)


def connect_redis(host: str, port: int):
    redis = try_import_redis()
    try:
        r = redis.Redis(host=host, port=port, decode_responses=True)
        r.ping()
        return r
    except Exception as exc:
        print(f"No se pudo conectar a Redis {host}:{port}: {exc}", file=sys.stderr)
        sys.exit(2)


def read_tag_value(r, tagid: int) -> Optional[float]:
    try:
        v = r.hget("HT_TAG_VALOR", str(tagid))
        if v is not None:
            return float(v)
    except Exception:
        pass
    try:
        v = r.get(f"HT_TAG_VALOR:{tagid}")
        if v is not None:
            return float(v)
    except Exception:
        pass
    return None


def count_tags_with_values(r) -> Tuple[int, int, int]:
    count_hash = 0
    try:
        for _field, value in r.hscan_iter("HT_TAG_VALOR"):
            try:
                float(value)
                count_hash += 1
            except Exception:
                continue
    except Exception:
        count_hash = 0

    count_keys = 0
    try:
        for key in r.scan_iter("HT_TAG_VALOR:*"):
            try:
                v = r.get(key)
                if v is not None:
                    float(v)
                    count_keys += 1
            except Exception:
                continue
    except Exception:
        count_keys = 0

    return count_hash, count_keys, count_hash + count_keys


def median_or_none(values: List[float]) -> Optional[float]:
    if not values:
        return None
    try:
        return float(statistics.median(values))
    except Exception:
        return None


def is_binary_values(values: List[float]) -> bool:
    if not values:
        return False
    rounded = {int(round(v)) for v in values}
    if not rounded.issubset({0, 1}):
        return False
    return all(abs(v - round(v)) <= 1e-6 for v in values)


def compute_active_seconds(
    samples_ts_vals: List[Tuple[datetime, float]], window_start: datetime, window_end: datetime
) -> float:
    if not samples_ts_vals:
        return 0.0
    samples = sorted(samples_ts_vals, key=lambda x: x[0])
    total_active = 0.0
    idx = 0
    current_ts = max(window_start, samples[0][0])
    current_state = 1 if float(round(samples[0][1])) >= 1.0 else 0

    while idx < len(samples) and samples[idx][0] < window_start:
        current_state = 1 if float(round(samples[idx][1])) >= 1.0 else 0
        idx += 1
    while idx < len(samples) and samples[idx][0] < window_end:
        next_ts = max(samples[idx][0], window_start)
        if next_ts > current_ts:
            delta = (next_ts - current_ts).total_seconds()
            if current_state == 1 and delta > 0:
                total_active += delta
            current_ts = next_ts
        current_state = 1 if float(round(samples[idx][1])) >= 1.0 else 0
        idx += 1

    if window_end > current_ts and current_state == 1:
        total_active += (window_end - current_ts).total_seconds()

    window_seconds = max(0.0, (window_end - window_start).total_seconds())
    return float(max(0.0, min(total_active, window_seconds)))


def _value_for_tag(ts_vals: list[tuple[datetime, float]], window_start: datetime, window_end: datetime) -> float | None:
    values_only = [v for (_ts, v) in ts_vals]
    if is_binary_values(values_only):
        return compute_active_seconds(ts_vals, window_start, window_end)
    return median_or_none(values_only)


def _flush_all_tags(
    *,
    duck_path: Path | None,
    outdir: Path | None,
    samples_by_tag: dict[int, list[tuple[datetime, float]]],
    window_start: datetime,
    window_end: datetime,
) -> tuple[int, int]:
    rows: list[tuple[int, float]] = []
    for tagid_i, ts_vals in sorted(samples_by_tag.items()):
        if not ts_vals:
            continue
        val = _value_for_tag(ts_vals, window_start, window_end)
        if val is None:
            continue
        rows.append((tagid_i, val))

    if duck_path is not None:
        saved = save_medians_duckdb(duck_path, window_end, rows)
        return len(rows), saved

    saved = 0
    for tagid_i, val in rows:
        save_median_all(outdir, tagid_i, window_end, val)
        saved += 1
    return len(rows), saved


def main() -> None:
    args = parse_args()
    duck_path: Path | None = Path(args.duckdb) if args.duckdb else None
    outdir: Path | None = None

    if not args.all_tags:
        if args.tagid is None:
            raise SystemExit("Debes indicar --tagid cuando no usas --all-tags.")
        if args.sqlite is None:
            raise SystemExit("Debes indicar --sqlite cuando no usas --all-tags.")
        tagid = int(args.tagid)
        table_name = args.table if args.table else f"tag_{tagid}"
        sqlite_path = Path(args.sqlite)
        setup_sqlite(sqlite_path, table_name)
    else:
        if duck_path is None and not args.outdir:
            raise SystemExit("Modo --all-tags requiere --duckdb o --outdir.")
        if duck_path is not None and args.outdir:
            print("[aviso] --duckdb activo; --outdir ignorado", file=sys.stderr)
        if duck_path is None:
            outdir = Path(args.outdir)
            outdir.mkdir(parents=True, exist_ok=True)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    r = connect_redis(args.redis_host, args.redis_port)
    step_minutes = max(1, int(args.window_minutes))
    sample_seconds = max(0.1, float(args.sample_seconds))

    now = datetime.now()
    window_start = floor_to_minutes(now, step_minutes)
    window_end = window_start + timedelta(minutes=step_minutes)
    samples: List[Tuple[datetime, float]] = []
    samples_by_tag: dict[int, List[Tuple[datetime, float]]] = {}

    if not args.all_tags:
        dest = f"sqlite={sqlite_path}"
    elif duck_path is not None:
        dest = f"duckdb={duck_path}"
    else:
        dest = f"outdir={outdir}"

    print(
        f"[inicio] {'ALL_TAGS' if args.all_tags else f'tagid={args.tagid}'} "
        f"redis={args.redis_host}:{args.redis_port} "
        f"ventana={window_start.isoformat(timespec='minutes')}..{window_end.isoformat(timespec='minutes')} "
        f"{dest}"
    )

    try:
        c_hash, c_keys, c_total = count_tags_with_values(r)
        print(f"[reporte] tags_con_valor hash={c_hash} keys={c_keys} total={c_total}")
    except Exception:
        pass

    first_now = datetime.now()
    if not args.all_tags:
        first_val = read_tag_value(r, int(args.tagid))
        if first_val is not None:
            samples.append((first_now, float(first_val)))
            print(f"[lectura-inicial] {first_now.isoformat(timespec='seconds')} valor={first_val}")
    else:
        try:
            for field, value in r.hscan_iter("HT_TAG_VALOR"):
                try:
                    tagid_i = int(str(field))
                    samples_by_tag.setdefault(tagid_i, []).append((first_now, float(value)))
                except Exception:
                    continue
            print(
                f"[lectura-inicial] tags={len(samples_by_tag)} "
                f"({first_now.isoformat(timespec='seconds')})"
            )
        except Exception:
            samples_by_tag = {}

    try:
        while not GracefulExit.exiting:
            now = datetime.now()
            if now >= window_end:
                if not args.all_tags:
                    val = _value_for_tag(samples, window_start, window_end)
                    if val is not None:
                        save_median_sqlite(sqlite_path, table_name, window_end, val)
                        print(
                            f"[mediana] ventana={window_start.isoformat(timespec='minutes')}.."
                            f"{window_end.isoformat(timespec='minutes')} "
                            f"fecha={window_end.isoformat(timespec='minutes')} valor={val:.6f}"
                        )
                    if args.once:
                        break
                else:
                    total_tags, total_saved = _flush_all_tags(
                        duck_path=duck_path,
                        outdir=outdir,
                        samples_by_tag=samples_by_tag or {},
                        window_start=window_start,
                        window_end=window_end,
                    )
                    print(
                        f"[cierre] ventana={window_start.isoformat(timespec='minutes')}.."
                        f"{window_end.isoformat(timespec='minutes')} "
                        f"fecha={window_end.isoformat(timespec='minutes')} "
                        f"tags={total_tags} guardados={total_saved}"
                    )
                    if args.once:
                        break

                window_start = floor_to_minutes(now, step_minutes)
                window_end = window_start + timedelta(minutes=step_minutes)
                samples = []
                samples_by_tag = {}
                print(
                    f"[siguiente] ventana={window_start.isoformat(timespec='minutes')}.."
                    f"{window_end.isoformat(timespec='minutes')}"
                )
                continue

            if not args.all_tags:
                val = read_tag_value(r, int(args.tagid))
                if val is not None:
                    samples.append((now, float(val)))
            else:
                added = 0
                try:
                    for field, value in r.hscan_iter("HT_TAG_VALOR"):
                        try:
                            tagid_i = int(str(field))
                            samples_by_tag.setdefault(tagid_i, []).append((now, float(value)))
                            added += 1
                        except Exception:
                            continue
                    if added > 0:
                        print(f"[lectura] {now.isoformat(timespec='seconds')} tags_actualizados={added}")
                except Exception:
                    pass
            time.sleep(sample_seconds)

        if not args.all_tags and samples:
            val = _value_for_tag(samples, window_start, window_end)
            if val is not None:
                save_median_sqlite(sqlite_path, table_name, window_end, val)
                print(f"[salida] fecha={window_end.isoformat(timespec='minutes')} guardada")
        elif args.all_tags and samples_by_tag:
            total_tags, total_saved = _flush_all_tags(
                duck_path=duck_path,
                outdir=outdir,
                samples_by_tag=samples_by_tag,
                window_start=window_start,
                window_end=window_end,
            )
            print(f"[salida] tags={total_tags} guardados={total_saved}")
    finally:
        pass


if __name__ == "__main__":
    main()
