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
echo "5) Reinsertando scripts de capas en index.html..."
python3 - <<'PY'
from pathlib import Path

p = Path("templates/index.html")
txt = p.read_text(encoding="utf-8")

target = '<script src="{{ url_for(\'static\', filename=\'js/portal.js\') }}?v=31"></script>'

scripts = [
    '<script src="{{ url_for(\'static\', filename=\'js/chart-layer-controls.js\') }}?v=1"></script>',
    '<script src="{{ url_for(\'static\', filename=\'js/step-hourly-layer.js\') }}?v=1"></script>',
]

for line in scripts:
    if line not in txt:
        txt = txt.replace(target, line + "\n  " + target)

p.write_text(txt, encoding="utf-8")
print("OK: scripts de capas cargados antes de portal.js")
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
