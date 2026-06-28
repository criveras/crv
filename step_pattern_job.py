#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime

from analyze import DEFAULT_CONFIG, load_config, prepare_dataset
from step_patterns import save_step_pattern
from variable_profiles import get_profile


def make_cfg(base, point, fini, ma):
    cfg = dict(base)
    cfg["point"] = point
    cfg["fini"] = fini
    cfg["ma"] = ma
    prof = get_profile(point, "", cfg)
    cfg["unit"] = prof.get("unit") or cfg.get("unit", "")
    return cfg


def main(args):
    base = load_config(DEFAULT_CONFIG) if DEFAULT_CONFIG.is_file() else {}
    points = args or list(dict.fromkeys(([base.get("point")] if base.get("point") else []) + list(base.get("preset_points") or [])))
    fini = base.get("step_pattern_fini", "*-365d")
    ma = int(base.get("step_pattern_ma", base.get("ma", 5)))
    print("inicio", datetime.now().isoformat(timespec="seconds"), "puntos", len(points))
    ok = 0
    for point in points:
        try:
            cfg = make_cfg(base, point, fini, ma)
            df, _, _ = prepare_dataset(cfg)
            path = save_step_pattern(df, point, cfg)
            print("OK", point, len(df), path)
            ok += 1
        except Exception as exc:
            print("ERROR", point, exc, file=sys.stderr)
    print("listo", ok, len(points))
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
