#!/usr/bin/env python3
from pathlib import Path

p = Path('app2.py')
s = p.read_text(encoding='utf-8')

repls = {
    'APP2_VERSION = "app2-v2026.06.28-005"': 'APP2_VERSION = "app2-v2026.06.28-006"',
    'DEFAULT_PATTERN_FINI = "*-365d"': 'DEFAULT_REAL_FINI = "*-30d"\nDEFAULT_PATTERN_FINI = "*-30d"',
    '"fini": "*-14d"': '"fini": DEFAULT_REAL_FINI',
    'value="*-14d"': 'value="{DEFAULT_REAL_FINI}"',
    "'#ff1744', 'fill': 'rgba(255,23,68,.12)'": "'#9c27b0', 'fill': 'rgba(156,39,176,.13)'",
    'style="background:#ff1744"></span>feriado Chile': 'style="background:#9c27b0"></span>feriado Chile',
    '4σ': '6σ',
    '4s': '6s',
    'll4': 'll6',
    'hh4': 'hh6',
    'segments4': 'segments6',
    'cur_points4': 'cur_points6',
    'make4SigmaSeries': 'make6SigmaSeries',
    'ALERTA 4': 'ALERTA 6',
    'LL 4': 'LL 6',
    'HH 4': 'HH 6',
    'data.steps.segments4': 'data.steps.segments6',
    'Alerta 3 puntos fuera 3σ': 'Alerta 3 puntos sobre HH 3σ',
    '3 puntos consecutivos fuera de 3σ': '3 puntos consecutivos sobre HH 3σ',
    'Punto rojo = 3 puntos consecutivos fuera de 3σ': 'Punto rojo = 3 puntos consecutivos sobre HH 3σ',
    'fini = request.args.get("fini") or base_cfg().get("fini", "*-14d")': 'fini = request.args.get("fini") or DEFAULT_REAL_FINI',
}
for a, b in repls.items():
    s = s.replace(a, b)

s = s.replace('outside3 = val < lim["ll3"] or val > lim["hh3"]', 'outside3 = val > lim["hh3"]')
s = s.replace('alertas ${{(data.alert_points||[]).length}}', 'alertas HH ${{(data.alert_points||[]).length}}')
s = s.replace('"default_pattern_fini": DEFAULT_PATTERN_FINI', '"default_real_fini": DEFAULT_REAL_FINI, "default_pattern_fini": DEFAULT_PATTERN_FINI')

# Evita doble insercion si el parche se ejecuta dos veces.
s = s.replace('DEFAULT_REAL_FINI = "*-30d"\nDEFAULT_REAL_FINI = "*-30d"\nDEFAULT_PATTERN_FINI = "*-30d"', 'DEFAULT_REAL_FINI = "*-30d"\nDEFAULT_PATTERN_FINI = "*-30d"')

p.write_text(s, encoding='utf-8')
print('OK app2.py parcheado a app2-v2026.06.28-006')
