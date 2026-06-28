#!/bin/bash
set -e

APP_DIR="/home/criveras/app/crv"
BRANCH="main"

echo "===================================="
echo " Actualizando CRV"
echo "===================================="

cd "$APP_DIR"

echo ""
echo "1) Eliminando temporales..."
rm -rf __pycache__
find . -type d -name "__pycache__" -prune -exec rm -rf {} +
find . -type f -name "*.pyc" -delete
find . -type f -name "*.pyo" -delete

echo ""
echo "2) Eliminando output/reportes..."
rm -rf output
mkdir -p output/reports

echo ""
echo "3) Descartando cambios locales que impiden pull..."
git restore templates/index.html 2>/dev/null || true
git restore output/ 2>/dev/null || true

echo ""
echo "4) Pull desde GitHub..."
git pull origin "$BRANCH"

echo ""
echo "5) Insertando automaticamente scripts JS auxiliares..."
python3 - <<'PY'
from pathlib import Path

p = Path("templates/index.html")
txt = p.read_text(encoding="utf-8")

target = '<script src="{{ url_for(\'static\', filename=\'js/portal.js\') }}?v=31"></script>'

patterns = [
    "*-layer.js",
    "*-controls.js",
    "auto-*.js",
]

names = []
for pattern in patterns:
    for js in sorted(Path("static/js").glob(pattern)):
        name = js.name
        if name in {"portal.js", "theme-toggle.js", "sigma-alarm-toggle.js"}:
            continue
        if name not in names:
            names.append(name)

if target not in txt:
    print("WARN: no encontre portal.js?v=31 en templates/index.html")
else:
    for name in names:
        line = '<script src="{{ url_for(\'static\', filename=\'js/' + name + '\') }}?v=1"></script>'
        if line not in txt:
            txt = txt.replace(target, line + "\n  " + target)
            print("OK: cargado", name)
        else:
            print("OK: ya estaba", name)

p.write_text(txt, encoding="utf-8")
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
