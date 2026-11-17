#!/usr/bin/env bash
# Rotina diária: cadastra linhas novas da planilha, coleta os preços e atualiza a planilha.
# Chamado pelo cron do Termux (veja README.md).
set -euo pipefail
cd "$(dirname "$0")"
. .venv/bin/activate
echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
python sheet_sync.py --import-only
python collect.py

# 2ª passada, com Chromium real, só para o que a 1ª não conseguiu (se o navegador estiver instalado).
if command -v xvfb-run >/dev/null 2>&1 && { command -v chromium >/dev/null 2>&1 || [ -n "${BROWSER_PATH:-}" ]; }; then
    xvfb-run -a python collect.py --browser --pending || echo "(navegador falhou; seguindo com o que foi coletado)"
fi

python sheet_sync.py
