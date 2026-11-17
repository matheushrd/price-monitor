#!/usr/bin/env bash
# Passo 2 de 2 — rode DENTRO do Debian, como seu usuário (não root):
#   proot-distro login debian --user matheus
#   bash ~/price-monitor/setup_python.sh
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-extra.txt || echo "(curl_cffi não instalou; sem problema, usarei requests)"

python - <<'PY'
import db
conn = db.connect(); db.init_schema(conn); conn.close()
print("Tabelas criadas.")
PY

if [ -f Monitoramento.csv ]; then
  python seed.py Monitoramento.csv
fi
mkdir -p logs
chmod +x run.sh
echo
echo "Pronto. Falta colocar o credentials.json aqui e testar: python collect.py --dry-run"
