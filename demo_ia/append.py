#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import logging
import sys
import time
from typing import Iterable, List

import app_ia as rt3ia


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Append automatico para recintos rt3-ia")
    parser.add_argument("--interval", type=int, default=60, help="Segundos entre ciclos")
    parser.add_argument("--paso-min", type=int, default=15, help="Resolucion en minutos para append")
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


def run_cycle(paso_min: int, only_names: List[str]) -> None:
    recintos = list(iter_target_recintos(only_names))
    if not recintos:
        logging.info("Sin recintos para procesar.")
        return

    logging.info("Iniciando ciclo append para %s recintos.", len(recintos))
    for cfg in recintos:
        try:
            result = rt3ia.run_append_for_recinto(cfg, paso_min=paso_min, source_label="append.py")
            status = str(result.get("status") or "unknown").upper()
            message = str(result.get("status_msg") or "")
            logging.info("[%s] %s", status, message)
            if str(result.get("status") or "").lower() in {"ok", "noop"}:
                snapshot = rt3ia.build_recinto_ia_snapshot(cfg.nombre)
                out_path = rt3ia.write_recinto_ia_snapshot(snapshot)
                if out_path is not None:
                    logging.info("[EXPORT] Snapshot IA guardado en %s", out_path)
                published = rt3ia.publish_qin_ideal_points(snapshot)
                if published:
                    logging.info("[POINT] Qin ideal publicado en: %s", ", ".join(published))
        except Exception:
            logging.exception("Fallo append automatico para recinto %s", cfg.nombre)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [append.py] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    only_names = selected_names(args.only)
    if only_names:
        logging.info("Modo filtrado para recintos: %s", ", ".join(only_names))
    else:
        logging.info("Modo automatico para todos los recintos activos.")

    try:
        while True:
            run_cycle(args.paso_min, only_names)
            if args.once:
                return 0
            time.sleep(max(1, int(args.interval)))
    except KeyboardInterrupt:
        logging.info("Append automatico detenido por teclado.")
        return 0
    except Exception:
        logging.exception("Error fatal en append.py")
        return 1


if __name__ == "__main__":
    sys.exit(main())
