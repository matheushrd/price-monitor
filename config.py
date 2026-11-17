"""Configuração lida de variáveis de ambiente ou do arquivo .env ao lado dos scripts."""
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file() -> None:
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://pricemon:pricemon@127.0.0.1:5432/pricemon")
SHEET_ID = os.environ.get("SHEET_ID", "1D1jZUfrecx6FA4qtnS_I3teeKddcBsTOeuYma20ucF4")
SHEET_TAB = os.environ.get("SHEET_TAB", "Monitoramento")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", str(BASE_DIR / "credentials.json"))
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Sao_Paulo"))

# Variação diária acima disso (35%) é tratada como suspeita: o preço não vai para a planilha
# até você confirmar com `python collect.py --approve ID`.
MAX_JUMP = Decimal(os.environ.get("MAX_JUMP", "0.35"))

# Pausa aleatória entre lojas, para não parecer um robô agressivo.
DELAY_MIN = float(os.environ.get("DELAY_MIN", "3"))
DELAY_MAX = float(os.environ.get("DELAY_MAX", "8"))


def today() -> date:
    return datetime.now(TZ).date()
