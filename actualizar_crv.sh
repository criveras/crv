#!/bin/bash
set -e

APP_DIR="/home/criveras/app/crv"
BRANCH="main"
REMOTE="origin"

echo "===================================="
echo " Actualizando CRV desde GitHub"
echo "===================================="

cd "$APP_DIR"

echo ""
echo "1) Limpieza previa de temporales..."
rm -rf __pycache__
find . -type d -name "__pycache__" -prune -exec rm -rf {} +
find . -type f -name "*.pyc" -delete
find . -type f -name "*.pyo" -delete
rm -rf output/reports

echo ""
echo "2) Sincronizando repo local como espejo de GitHub..."
echo "   Fuente oficial: $REMOTE/$BRANCH"
git fetch "$REMOTE" "$BRANCH"
git reset --hard "$REMOTE/$BRANCH"

echo ""
echo "3) Limpiando archivos no versionados..."
echo "   Se conserva: venv, .env, logs, output y patrones aprendidos"
git clean -fd -e venv -e venv/ -e .env -e app.log -e "*.log" -e output -e output/
mkdir -p output/reports output/patterns

echo ""
echo "4) Activando backend de patrones LL/HH step..."
python3 - <<'PY'
from pathlib import Path

p = Path("app.py")
txt = p.read_text(encoding="utf-8")

imp = "from step_patterns import build_step_overlay\n"
anchor = "from sixsigma import apply_sigma_alarm_limits, apply_sigma_lh_limits, build_sigma_bands, detect_patterns, pattern_markers\n"
if imp not in txt:
    txt = txt.replace(anchor, anchor + imp)
    print("OK: import build_step_overlay agregado")
else:
    print("OK: import build_step_overlay ya existe")

block = """\n    step_overlay = build_step_overlay(point, df, cfg)\n    if step_overlay:\n        payload[\"step_patterns\"] = step_overlay\n"""
if "payload[\"step_patterns\"]" not in txt:
    marker = "    if is_volume_point(point):\n"
    txt = txt.replace(marker, block + marker)
    print("OK: payload step_patterns agregado")
else:
    print("OK: payload step_patterns ya existe")

p.write_text(txt, encoding="utf-8")
PY

echo ""
echo "5) Cargando automaticamente JS auxiliares..."
echo "   Orden: *-layer.js, *-controls.js, auto-*.js"
python3 - <<'PY'
from pathlib import Path

html = Path("templates/index.html")
text = html.read_text(encoding="utf-8")

target = '<script src="{{ url_for(\'static\', filename=\'js/portal.js\') }}?v=31"></script>'
js_dir = Path("static/js")
patterns = ["*-layer.js", "*-controls.js", "auto-*.js"]
skip = {"portal.js", "theme-toggle.js", "sigma-alarm-toggle.js"}

names = []
for pattern in patterns:
    for js in sorted(js_dir.glob(pattern)):
        name = js.name
        if name in skip:
            continue
        if name not in names:
            names.append(name)

clean = []
for line in text.splitlines():
    remove = any(("url_for" in line and ("js/" + name) in line) for name in names)
    if not remove:
        clean.append(line)

out = []
inserted = False
for line in clean:
    if not inserted and line.strip() == target:
        indent = line[:len(line) - len(line.lstrip())]
        for name in names:
            src = '<script src="{{ url_for(\'static\', filename=\'js/' + name + '\') }}?v=1"></script>'
            out.append(indent + src)
            print("OK: cargado", name)
        inserted = True
    out.append(line)

if not inserted:
    print("WARN: no encontre portal.js?v=31 en templates/index.html")

html.write_text("\n".join(out) + "\n", encoding="utf-8")
PY

echo ""
echo "6) Verificando venv..."
if [ ! -x "venv/bin/python3" ]; then
    echo "Creando venv..."
    python3 -m venv venv
else
    echo "venv OK"
fi

echo ""
echo "7) Activando venv..."
source venv/bin/activate

echo ""
echo "8) Instalando dependencias..."
pip install -r requirements.txt

echo ""
echo "9) Verificacion rapida:"
grep -E "step-hourly-layer|chart-layer-controls|auto-step" templates/index.html || true
grep -n "step_patterns\|build_step_overlay" app.py || true

echo ""
echo "10) Estado Git:"
git status --short

echo ""
echo "===================================="
echo " Listo"
echo " Ejecutar app:"
echo " PORT=5092 python3 app.py"
echo ""
echo " Precalcular patrones una vez al dia:"
echo " venv/bin/python3 step_pattern_job.py"
echo ""
echo " Cron sugerido:"
echo " 10 3 * * * cd $APP_DIR && $APP_DIR/venv/bin/python3 step_pattern_job.py >> output/step_patterns.log 2>&1"
echo ""
echo " Con RT3:"
echo " RT3_API_HOST=http://100.x.x.x:8090 PORT=5092 python3 app.py"
echo "===================================="
