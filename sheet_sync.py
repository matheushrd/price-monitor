"""Atualiza a planilha do Google com os preços coletados.

    python sheet_sync.py                    # grava o dia de hoje na planilha
    python sheet_sync.py --dry-run          # mostra o que mudaria, sem escrever
    python sheet_sync.py --import-only      # só cadastra no banco as linhas novas da planilha

Como a planilha é atualizada (formato "uma coluna por dia"):
  * A coluna "Preço Atual em DD/MM/AAAA" guarda o dia mais recente.
  * Quando o dia muda, ela é *arquivada*: uma coluna nova com a data antiga é inserida logo antes
    dela (com os mesmos valores), e a coluna "Preço Atual" passa a mostrar o dia de hoje.
  * Status Diário, Variação Acumulada, Parcelado Total, Parcelas, Avaliação e Qtd Avaliações são
    atualizados. Colunas que forem fórmulas na planilha não são sobrescritas.
  * Produto com preço suspeito, esgotado ou com falha na coleta mantém o último preço bom e o
    Status Diário mostra "Verificar", "Indisponível" ou "Falha na coleta".
  * As colunas Loja, Link Direto e Observação nunca são alteradas.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from gspread.utils import ValueRenderOption, rowcol_to_a1

import config
from extract import parse_brl

LABEL_DATE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
FLAG_LABEL = {"suspeito": "Verificar", "indisponivel": "Indisponível", "erro": "Falha na coleta"}
CURRENCY = {"numberFormat": {"type": "CURRENCY", "pattern": '"R$" #,##0.00'}}
PERCENT = {"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}}


@dataclass
class Report:
    archived: Optional[date] = None
    cells_written: int = 0
    formula_columns_skipped: list[str] = field(default_factory=list)
    unmatched_sheet_rows: list[str] = field(default_factory=list)
    missing_in_sheet: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)


def to_number(v: Any) -> Optional[Decimal]:
    if v is None or v == "" or isinstance(v, bool):
        return None
    return parse_brl(v)


def find_col(header: list[Any], startswith: str) -> int:
    for i, h in enumerate(header):
        if str(h).strip().startswith(startswith):
            return i
    raise KeyError(f"Coluna '{startswith}' não encontrada na planilha")


def _pad(grid: list[list[Any]]) -> list[list[Any]]:
    width = max(len(r) for r in grid)
    return [list(r) + [""] * (width - len(r)) for r in grid]


def status_label(price: Optional[Decimal], prev: Optional[Decimal], flag: Optional[str]) -> str:
    if flag:
        return FLAG_LABEL[flag]
    if price is None or prev is None:
        return "Estável"
    if price < prev - Decimal("0.005"):
        return "Caiu"
    if price > prev + Decimal("0.005"):
        return "Subiu"
    return "Estável"


def _num(d: Optional[Decimal]) -> Optional[float]:
    return None if d is None else float(d)


def apply_to_sheet(ws, data: dict[str, dict], today: date, dry_run: bool = False) -> Report:
    """data: url -> {price, prev, flag, parcelado, parcelas, rating, reviews, valor_inicial}"""
    rep = Report()
    values = _pad(ws.get_all_values(value_render_option=ValueRenderOption.unformatted))
    formulas = _pad(ws.get_all_values(value_render_option=ValueRenderOption.formula))
    header = values[0]
    name_col, link_col = find_col(header, "Produto"), find_col(header, "Link Direto")
    last = max((r for r in range(1, len(values)) if str(values[r][name_col]).strip()), default=0)
    if last == 0:
        return rep

    # 1) Arquivar o dia anterior, se a coluna "Preço Atual" ainda mostra outra data.
    cur = find_col(header, "Preço Atual")
    m = LABEL_DATE.search(str(header[cur]))
    cur_date = date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
    if cur_date and cur_date < today:
        col_vals = [f"{cur_date:%d/%m/%Y}"] + [values[r][cur] for r in range(1, last + 1)]
        if not dry_run:
            ws.insert_cols([col_vals], col=cur + 1, inherit_from_before=True)  # RAW: não reinterpreta textos/datas
        for grid in (values, formulas):
            for r, row in enumerate(grid):
                row.insert(cur, col_vals[r] if r <= last else "")
        rep.archived = cur_date
        header = values[0]

    cols = {
        "price": find_col(header, "Preço Atual"),
        "ini": find_col(header, "Valor Inicial"),
        "status": find_col(header, "Status"),
        "var_rs": find_col(header, "Variação Acumulada R$"),
        "var_pct": find_col(header, "Variação Acumulada %"),
        "parc": find_col(header, "Parcelado Total"),
        "n": find_col(header, "Parcelas"),
        "aval": find_col(header, "Avalia"),
        "qtd": find_col(header, "Qtd Avalia"),
    }
    name_col, link_col = find_col(header, "Produto"), find_col(header, "Link Direto")

    def is_formula_col(col: int) -> bool:
        return any(str(formulas[r][col]).startswith("=") for r in range(1, last + 1))

    skip = {k for k in ("status", "var_rs", "var_pct", "parc", "n", "aval", "qtd") if is_formula_col(cols[k])}
    rep.formula_columns_skipped = [str(header[cols[k]]) for k in sorted(skip)]

    # 2) Montar as células a escrever (uma por produto e coluna).
    updates: list[dict] = [{
        "range": rowcol_to_a1(1, cols["price"] + 1),
        "values": [[f"Preço Atual em {today:%d/%m/%Y}"]],
    }]
    seen: set[str] = set()
    for r in range(1, last + 1):
        url = str(values[r][link_col]).strip()
        d = data.get(url)
        if d is None:
            rep.unmatched_sheet_rows.append(str(values[r][name_col]))
            continue
        seen.add(url)
        price, prev, flag = d["price"], d["prev"], d["flag"]
        cells: dict[str, Any] = {}
        if price is not None:
            cells["price"] = _num(price)
        cells["status"] = status_label(price, prev, flag)
        base = to_number(values[r][cols["ini"]]) or d.get("valor_inicial")
        if price is not None and base:
            cells["var_rs"] = _num(price - base)
            cells["var_pct"] = float((price - base) / base)
        if flag is None:
            cells["parc"], cells["n"] = _num(d.get("parcelado")), d.get("parcelas")
            cells["aval"], cells["qtd"] = _num(d.get("rating")), d.get("reviews")
        if flag:
            rep.flagged.append(f"{values[r][name_col]} ({FLAG_LABEL[flag]})")
        for key, val in cells.items():
            if key in skip or val is None:
                continue
            updates.append({"range": rowcol_to_a1(r + 1, cols[key] + 1), "values": [[val]]})
    rep.missing_in_sheet = [u for u in data if u not in seen]
    rep.cells_written = len(updates)

    # 3) Gravar tudo de uma vez.
    if not dry_run:
        ws.batch_update(updates)  # RAW (padrão do gspread): números ficam números
        rng = lambda key: f"{rowcol_to_a1(2, cols[key] + 1)}:{rowcol_to_a1(last + 1, cols[key] + 1)}"
        formats = [{"range": rng("price"), "format": CURRENCY}]
        if "var_rs" not in skip:
            formats.append({"range": rng("var_rs"), "format": CURRENCY})
        if "var_pct" not in skip:
            formats.append({"range": rng("var_pct"), "format": PERCENT})
        if "parc" not in skip:
            formats.append({"range": rng("parc"), "format": CURRENCY})
        ws.batch_format(formats)
    return rep


def load_row_data(conn, day: date) -> dict[str, dict]:
    """Lê do banco, por link, o que deve aparecer na planilha em `day`."""
    from db import dict_cursor

    sql = """
    WITH today AS (SELECT * FROM price_snapshots WHERE data = %(d)s),
    good AS (SELECT DISTINCT ON (product_id) product_id, preco_pix FROM price_snapshots
             WHERE status='ok' AND preco_pix IS NOT NULL AND data <= %(d)s
             ORDER BY product_id, data DESC),
    prev AS (SELECT DISTINCT ON (product_id) product_id, preco_pix FROM price_snapshots
             WHERE status='ok' AND preco_pix IS NOT NULL AND data < %(d)s
             ORDER BY product_id, data DESC)
    SELECT p.url, p.valor_inicial, t.status AS t_status, t.parcelado_total, t.parcelas,
           t.avaliacao, t.qtd_avaliacoes, g.preco_pix AS price, pr.preco_pix AS prev
    FROM products p
    LEFT JOIN today t ON t.product_id = p.id
    LEFT JOIN good g  ON g.product_id = p.id
    LEFT JOIN prev pr ON pr.product_id = p.id
    WHERE p.ativo"""
    out: dict[str, dict] = {}
    with dict_cursor(conn) as cur:
        cur.execute(sql, {"d": day})
        for r in cur.fetchall():
            flag = r["t_status"] if r["t_status"] != "ok" else None
            if r["t_status"] is None:
                flag = "erro"  # o coletor não rodou para esse produto hoje
            out[r["url"].strip()] = {
                "price": r["price"], "prev": r["prev"], "flag": flag,
                "valor_inicial": r["valor_inicial"], "parcelado": r["parcelado_total"],
                "parcelas": r["parcelas"], "rating": r["avaliacao"], "reviews": r["qtd_avaliacoes"],
            }
    return out


def import_new_products(ws, conn) -> int:
    """Cadastra no banco linhas da planilha cujo link ainda não existe."""
    grid = _pad(ws.get_all_values())
    h = grid[0]
    c = {k: find_col(h, v) for k, v in {
        "cat": "Categoria", "nome": "Produto", "tensao": "Tens", "ini": "Valor Inicial",
        "loja": "Loja", "link": "Link Direto"}.items()}
    added = 0
    with conn.cursor() as cur:
        for row in grid[1:]:
            url = str(row[c["link"]]).strip()
            if not url.startswith("http"):
                continue
            cur.execute(
                """INSERT INTO products (categoria, nome, tensao, loja, url, valor_inicial)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (url) DO NOTHING""",
                (row[c["cat"]], row[c["nome"]], row[c["tensao"]], row[c["loja"]], url,
                 parse_brl(row[c["ini"]])),
            )
            added += cur.rowcount
    conn.commit()
    return added


def main() -> int:
    import gspread

    import db

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--import-only", action="store_true")
    ap.add_argument("--date", help="AAAA-MM-DD (padrão: hoje)")
    args = ap.parse_args()
    day = date.fromisoformat(args.date) if args.date else config.today()

    gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS)
    ws = gc.open_by_key(config.SHEET_ID).worksheet(config.SHEET_TAB)
    conn = db.connect()

    if args.import_only:
        print(f"{import_new_products(ws, conn)} produtos novos cadastrados.")
        return 0

    rep = apply_to_sheet(ws, load_row_data(conn, day), day, dry_run=args.dry_run)
    conn.close()
    print(("[dry-run] " if args.dry_run else "") + f"Planilha atualizada para {day:%d/%m/%Y}: {rep.cells_written} células.")
    if rep.archived:
        print(f"  coluna do dia {rep.archived:%d/%m/%Y} arquivada antes de 'Preço Atual'")
    if rep.formula_columns_skipped:
        print("  colunas com fórmula, não alteradas: " + ", ".join(rep.formula_columns_skipped))
    if rep.flagged:
        print("  ATENÇÃO, precisam de conferência: " + "; ".join(rep.flagged))
    if rep.unmatched_sheet_rows:
        print("  linhas da planilha sem cadastro no banco (rode --import-only): " + "; ".join(rep.unmatched_sheet_rows))
    if rep.missing_in_sheet:
        print(f"  {len(rep.missing_in_sheet)} produtos do banco não estão na planilha")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
