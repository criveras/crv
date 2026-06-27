#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import logging
import math
import sys
import time
from typing import Iterable, List

import app_ia as rt3ia


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Exporta snapshots IA por recinto a JSON")
    parser.add_argument("--interval", type=int, default=60, help="Segundos entre ciclos")
    parser.add_argument(
        "--offset-sec",
        type=int,
        default=50,
        help="Segundo dentro del minuto para disparar la exportacion",
    )
    parser.add_argument("--once", action="store_true", help="Ejecuta un solo ciclo y termina")
    parser.add_argument(
        "--only",
        type=str,
        default="",
        help="Lista separada por comas de recintos a procesar; por defecto usa todos los activos",
    )
    return parser.parse_args()


def selected_names(raw: str) -> List[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def iter_target_recintos(only_names: List[str]) -> Iterable[rt3ia.RecintoConfig]:
    recintos = rt3ia.load_all_recintos()
    if only_names:
        selected = set(only_names)
        return [cfg for cfg in recintos if cfg.nombre in selected]
    return [cfg for cfg in recintos if cfg.activo]


def run_cycle(only_names: List[str]) -> None:
    recintos = list(iter_target_recintos(only_names))
    if not recintos:
        logging.info("Sin recintos para exportar.")
        return

    logging.info("Iniciando ciclo export JSON para %s recintos.", len(recintos))
    for cfg in recintos:
        try:
            snapshot = rt3ia.build_recinto_ia_snapshot(cfg.nombre)
            out_path = rt3ia.write_recinto_ia_snapshot(snapshot)
            if out_path is not None:
                logging.info("[OK] Snapshot IA guardado en %s", out_path)
            published = rt3ia.publish_qin_ideal_points(snapshot)
            if published:
                logging.info("[POINT] Qin ideal publicado en: %s", ", ".join(published))
            else:
                logging.info("[SKIP] No se pudo construir snapshot para %s", cfg.nombre)
        except Exception:
            logging.exception("Fallo export JSON para recinto %s", cfg.nombre)


def sleep_until_next_cycle(interval: int, offset_sec: int) -> None:
    now = time.time()
    interval = max(1, int(interval))
    offset_sec = max(0, min(int(offset_sec), interval - 1))
    base = math.floor(now / interval) * interval
    next_ts = base + offset_sec
    if next_ts <= now:
        next_ts += interval
    delay = max(1.0, next_ts - now)
    time.sleep(delay)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [export_snapshots.py] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    only_names = selected_names(args.only)
    if only_names:
        logging.info("Modo filtrado para recintos: %s", ", ".join(only_names))
    else:
        logging.info("Modo automatico para todos los recintos activos.")

    try:
        while True:
            if not args.once:
                sleep_until_next_cycle(args.interval, args.offset_sec)
            run_cycle(only_names)
            if args.once:
                return 0
    except KeyboardInterrupt:
        logging.info("Export JSON detenido por teclado.")
        return 0
    except Exception:
        logging.exception("Error fatal en export_snapshots.py")
        return 1


if __name__ == "__main__":
    sys.exit(main())
