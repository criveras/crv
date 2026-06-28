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
rm -rf output

echo ""
echo "2) Sincronizando repo local como espejo de GitHub..."
echo "   Fuente oficial: $REMOTE/$BRANCH"
git fetch "$REMOTE" "$BRANCH"
git reset --hard "$REMOTE/$BRANCH"

echo ""
echo "3) Limpiando archivos no versionados..."
echo "   Se conserva: venv, .env, logs y output"
git clean -fd -e venv -e venv/ -e .env -e app.log -e "*.log" -e output -e output/
mkdir -p output/reports

echo ""
echo "4) Cargando automaticamente JS auxiliares..."
echo "   Orden: *-layer.js, *-controls.js, auto-*.js"
python3 - <<'PY'
from pathlib import Path

html = Path("templates/index.html")
text = html.read_text(encoding="utf-8")

target = '<script src="{{ url_for(\'static\', filename=\'js/portal.js\') }}?v=31"></script>'
js_dir = Path("static/js")

patterns = [
    "*-layer.js",
    "*-controls.js",
    "auto-*.js",
]

skip = {
    "portal.js",
    "theme-toggle.js",
    "sigma-alarm-toggle.js",
}

names = []
for pattern in patterns:
    for js in sorted(js_dir.glob(pattern)):
        name = js.name
        if name in skip:
            continue
        if name not in names:
            names.append(name)

# Borra cualquier linea previa que cargue estos JS, aunque tenga espacios distintos.
lines = text.splitlines()
clean = []
for line in lines:
    remove = False
    for name in names:
        if "url_for" in line and ("js/" + name) in line:
            remove = True
            break
    if not remove:
        clean.append(line)

out = []
inserted = False
for line in clean:
    stripped = line.strip()
    if not inserted and stripped == target:
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
echo "5) Verificando venv..."
if [ ! -x "venv/bin/python3" ]; then
    echo "Creando venv..."
    python3 -m venv venv
else
    echo "venv OK"
fi

echo ""
echo "6) Activando venv..."
source venv/bin/activate

echo ""
echo "7) Instalando dependencias..."
pip install -r requirements.txt

echo ""
echo "8) Verificacion rapida de JS cargados:"
grep -E "step-hourly-layer|chart-layer-controls|auto-step-holiday-fix" templates/index.html || true

echo ""
echo "9) Estado Git:"
git status --short

echo ""
echo "===================================="
echo " Listo"
echo " Ejecutar:"
echo " PORT=5092 python3 app.py"
echo ""
echo " Con RT3:"
echo " RT3_API_HOST=http://100.x.x.x:8090 PORT=5092 python3 app.py"
echo "===================================="
