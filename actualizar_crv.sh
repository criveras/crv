#!/bin/bash
set -e

APP_DIR="/home/criveras/app/crv"
BRANCH="main"

echo "Entrando a $APP_DIR"
cd "$APP_DIR"

echo "Limpiando archivos generados locales..."
git restore output/ 2>/dev/null || true
rm -rf __pycache__

echo "Actualizando desde GitHub..."
git pull origin "$BRANCH"

echo "Verificando entorno virtual..."
if [ ! -x "venv/bin/python3" ]; then
    echo "Creando venv..."
    python3 -m venv venv
fi

echo "Activando venv..."
source venv/bin/activate

echo "Instalando/actualizando dependencias..."
pip install -r requirements.txt

echo "Estado Git:"
git status --short

echo ""
echo "Listo. Para ejecutar:"
echo "source venv/bin/activate"
echo "PORT=5092 python3 app.py"
echo ""
echo "O con RT3:"
echo "RT3_API_HOST=http://100.x.x.x:8090 PORT=5092 python3 app.py"
