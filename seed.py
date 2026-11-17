"""Importa produtos e histórico de preços do CSV exportado da planilha.

Uso:
    python seed.py caminho/para/Monitoramento.csv

O arquivo pode ser o CSV exportado do Google Sheets (aba Monitoramento). Se tiver a linha
'## Sheet name: ...' no topo, ela é ignorada, assim como outras abas depois da primeira.
É seguro rodar de novo: produtos são identificados pelo link e snapshots por (produto, dia).
"""
from __future__ import annotations

import csv
import io
import re
import sys
from datetime import date
from decimal import Decimal
from typing import Optional

import db
from extract import parse_brl

DATE_HEADER = re.compile(r"^(\d{2})/(\d{2})(?:/(\d{4}))?$")
LABEL_DATE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def read_monitoring_rows(path: str) -> tuple[list[str], list[list[str]]]:
    lines = open(path, encoding="utf-8-sig").read().splitlines()
    block: list[str] = []
    started = False
    for line in lines:
        if line.startswith("## "):
            if started:
                break
            started = True
            continue
        if not line.strip():
            if block:
                break
            continue
        block.append(line)
    rows = list(csv.reader(io.StringIO("\n".join(block))))
    return rows[0], [r for r in rows[1:] if any(c.strip() for c in r)]


def _label_date(header: str) -> Optional[date]:
    m = LABEL_DATE.search(header)
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def _col(header: list[str], startswith: str) -> int:
    for i, h in enumerate(header):
        if h.strip().startswith(startswith):
            return i
    raise KeyError(f"coluna '{startswith}' não encontrada")


def _int(v: str) -> Optional[int]:
    v = v.strip().replace(".", "")
    return int(v) if v.isdigit() else None


def seed(path: str) -> None:
    header, rows = read_monitoring_rows(path)
    c = {
        "cat": _col(header, "Categoria"), "nome": _col(header, "Produto"), "tensao": _col(header, "Tens"),
        "ini": _col(header, "Valor Inicial"), "atual": _col(header, "Preço Atual"),
        "parc": _col(header, "Parcelado Total"), "n": _col(header, "Parcelas"),
        "aval": _col(header, "Avalia"), "qtd": _col(header, "Qtd Avalia"),
        "loja": _col(header, "Loja"), "link": _col(header, "Link Direto"),
    }
    ini_date = _label_date(header[c["ini"]])
    atual_date = _label_date(header[c["atual"]])
    year = ini_date.year if ini_date else date.today().year

    history_cols: list[tuple[int, date]] = []
    for i, h in enumerate(header):
        m = DATE_HEADER.match(h.strip())
        if m:
            history_cols.append((i, date(int(m.group(3) or year), int(m.group(2)), int(m.group(1)))))

    conn = db.connect()
    db.init_schema(conn)
    n_prod = n_snap = 0
    with conn.cursor() as cur:
        for r in rows:
            r = r + [""] * (len(header) - len(r))
            url = r[c["link"]].strip()
            if not url:
                continue
            cur.execute(
                """INSERT INTO products (categoria, nome, tensao, loja, url, valor_inicial)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (url) DO UPDATE SET categoria=EXCLUDED.categoria, nome=EXCLUDED.nome,
                       tensao=EXCLUDED.tensao, loja=EXCLUDED.loja, valor_inicial=EXCLUDED.valor_inicial
                   RETURNING id""",
                (r[c["cat"]], r[c["nome"]], r[c["tensao"]], r[c["loja"]], url, parse_brl(r[c["ini"]])),
            )
            pid = cur.fetchone()[0]
            n_prod += 1

            series: list[tuple[date, Optional[Decimal]]] = []
            if ini_date:
                series.append((ini_date, parse_brl(r[c["ini"]])))
            series += [(d, parse_brl(r[i])) for i, d in history_cols]
            if atual_date:
                series.append((atual_date, parse_brl(r[c["atual"]])))

            for d, price in series:
                if price is None:
                    continue
                extra = atual_date == d
                cur.execute(
                    """INSERT INTO price_snapshots
                         (product_id, data, preco_pix, parcelado_total, parcelas, avaliacao, qtd_avaliacoes,
                          status, origem)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,'ok','planilha')
                       ON CONFLICT (product_id, data) DO UPDATE SET preco_pix=EXCLUDED.preco_pix""",
                    (
                        pid, d, price,
                        parse_brl(r[c["parc"]]) if extra else None,
                        _int(r[c["n"]]) if extra else None,
                        parse_brl(r[c["aval"]]) if extra else None,
                        _int(r[c["qtd"]]) if extra else None,
                    ),
                )
                n_snap += 1
    conn.commit()
    conn.close()
    print(f"{n_prod} produtos e {n_snap} preços históricos importados.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    seed(sys.argv[1])
