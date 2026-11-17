import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from gspread.utils import a1_to_rowcol

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import seed  # noqa: E402
import sheet_sync as ss  # noqa: E402
from extract import parse_brl  # noqa: E402

CSV = next(Path("/mnt/user-data/uploads").glob("*Monitoramento*"), None)
D = Decimal


class FakeWorksheet:
    """Simula só o que o sync usa do gspread. `grid` guarda valores como a API 'unformatted' devolve."""

    def __init__(self, grid, formulas=None):
        self.grid = [list(r) for r in grid]
        self.formulas = formulas
        self.formats = []
        self.calls = []

    def get_all_values(self, value_render_option=None, **_):
        if value_render_option is not None and "formula" in str(value_render_option).lower():
            src = self.formulas or self.grid
        else:
            src = self.grid
        return [list(r) for r in src]

    def insert_cols(self, values, col=1, value_input_option=None, inherit_from_before=False):
        self.calls.append("insert_cols")
        new = values[0]
        for i, row in enumerate(self.grid):
            row.insert(col - 1, new[i] if i < len(new) else "")
        if self.formulas:
            for i, row in enumerate(self.formulas):
                row.insert(col - 1, new[i] if i < len(new) else "")

    def batch_update(self, data, value_input_option=None):
        self.calls.append("batch_update")
        for item in data:
            r, c = a1_to_rowcol(item["range"])
            self.grid[r - 1][c - 1] = item["values"][0][0]

    def batch_format(self, formats):
        self.calls.append("batch_format")
        self.formats += formats

    # helpers de teste
    def col(self, startswith):
        return ss.find_col(self.grid[0], startswith)

    def cell(self, row, startswith):
        return self.grid[row][self.col(startswith)]


NUM_COLS = ("Valor Inicial", "18/10", "21/10", "24/10", "25/10", "26/10", "27/10", "28/10", "29/10",
            "30/10", "31/10", "01/11", "Preço Atual", "Variação", "Parcelado Total")


def sheet_from_csv():
    header, rows = seed.read_monitoring_rows(str(CSV))
    grid = [header]
    for r in rows:
        r = r + [""] * (len(header) - len(r))
        out = []
        for h, v in zip(header, r):
            if h.startswith(NUM_COLS) and v.strip():
                out.append(float(parse_brl(v)))
            else:
                out.append(v)
        grid.append(out)
    return grid


def data_for(grid, overrides=None):
    overrides = overrides or {}
    """Um registro por link, com o preço atual da planilha, e ajustes por número de linha."""
    h = grid[0]
    link, price = ss.find_col(h, "Link Direto"), ss.find_col(h, "Preço Atual")
    out = {}
    for i, row in enumerate(grid[1:], start=1):
        p = D(str(row[price]))
        out[row[link]] = {"price": p, "prev": p, "flag": None, "valor_inicial": None,
                          "parcelado": p * D("1.05"), "parcelas": 10, "rating": D("4.8"), "reviews": 99}
        out[row[link]].update(overrides.get(i, {}))
    return out


@pytest.mark.skipif(CSV is None, reason="CSV de exemplo não está disponível")
def test_csv_is_read_with_28_products():
    header, rows = seed.read_monitoring_rows(str(CSV))
    assert len(rows) == 28
    assert header[1] == "Produto" and header[-1] == "Observação"


@pytest.mark.skipif(CSV is None, reason="CSV de exemplo não está disponível")
def test_archives_previous_day_then_writes_today():
    grid = sheet_from_csv()
    ws = FakeWorksheet(grid)
    old_price_col = [r[ws.col("Preço Atual")] for r in grid[1:]]
    n_cols_before = len(grid[0])

    data = data_for(grid, {
        1: {"price": D("4800.00"), "prev": D("5038.80")},                    # caiu
        2: {"price": D("4200.00"), "prev": D("4095.00")},                    # subiu
        3: {"flag": "suspeito", "price": D("4304.48"), "prev": D("4304.48")},  # carrega último preço bom
        4: {"flag": "indisponivel"},
    })
    rep = ss.apply_to_sheet(ws, data, date(2026, 11, 16))

    assert rep.archived == date(2026, 11, 2)
    assert len(ws.grid[0]) == n_cols_before + 1
    arch = ws.col("02/11/2026")
    assert ws.grid[0][arch] == "02/11/2026"
    assert [r[arch] for r in ws.grid[1:]] == old_price_col      # valores antigos preservados
    assert ws.grid[0][arch + 1] == "Preço Atual em 16/11/2026"  # coluna atual fica logo depois

    assert ws.cell(1, "Preço Atual") == 4800.0
    assert ws.cell(1, "Status") == "Caiu"
    assert ws.cell(2, "Status") == "Subiu"
    assert ws.cell(3, "Status") == "Verificar"
    assert ws.cell(3, "Preço Atual") == 4304.48                 # preço suspeito NÃO entrou
    assert ws.cell(4, "Status") == "Indisponível"
    assert ws.cell(5, "Status") == "Estável"
    # variação acumulada contra o Valor Inicial da planilha (5163,38 na linha 1)
    assert ws.cell(1, "Variação Acumulada R$") == pytest.approx(4800 - 5163.38)
    assert ws.cell(1, "Variação Acumulada %") == pytest.approx((4800 - 5163.38) / 5163.38)
    assert ws.cell(5, "Parcelas") == 10 and ws.cell(5, "Qtd Avalia") == 99
    # produto com flag mantém parcelado/avaliação antigos
    assert ws.cell(3, "Parcelas") == grid[3][ss.find_col(grid[0], "Parcelas")]
    assert rep.flagged and "Indisponível" in " ".join(rep.flagged)
    assert "insert_cols" in ws.calls and "batch_format" in ws.calls


@pytest.mark.skipif(CSV is None, reason="CSV de exemplo não está disponível")
def test_same_day_rerun_does_not_archive_again():
    grid = sheet_from_csv()
    ws = FakeWorksheet(grid)
    data = data_for(grid)
    ss.apply_to_sheet(ws, data, date(2026, 11, 16))
    width = len(ws.grid[0])
    rep = ss.apply_to_sheet(ws, data, date(2026, 11, 16))
    assert rep.archived is None and len(ws.grid[0]) == width
    assert ws.grid[0].count("02/11/2026") == 1


@pytest.mark.skipif(CSV is None, reason="CSV de exemplo não está disponível")
def test_next_day_archives_yesterday():
    grid = sheet_from_csv()
    ws = FakeWorksheet(grid)
    ss.apply_to_sheet(ws, data_for(grid), date(2026, 11, 16))
    ss.apply_to_sheet(ws, data_for(grid), date(2026, 11, 17))
    assert "16/11/2026" in ws.grid[0] and "02/11/2026" in ws.grid[0]
    assert ws.grid[0][ws.col("Preço Atual")] == "Preço Atual em 17/11/2026"


@pytest.mark.skipif(CSV is None, reason="CSV de exemplo não está disponível")
def test_formula_columns_are_not_overwritten():
    grid = sheet_from_csv()
    formulas = [list(r) for r in grid]
    rs, pct = ss.find_col(grid[0], "Variação Acumulada R$"), ss.find_col(grid[0], "Variação Acumulada %")
    for r in range(1, len(grid)):
        formulas[r][rs], formulas[r][pct] = f"=P{r+1}-D{r+1}", f"=R{r+1}/D{r+1}"
    ws = FakeWorksheet(grid, formulas)
    before = [(r[rs], r[pct]) for r in grid[1:]]
    rep = ss.apply_to_sheet(ws, data_for(grid, {1: {"price": D("1.00")}}), date(2026, 11, 16))
    rs2, pct2 = ws.col("Variação Acumulada R$"), ws.col("Variação Acumulada %")
    assert [(r[rs2], r[pct2]) for r in ws.grid[1:]] == before
    assert set(rep.formula_columns_skipped) == {"Variação Acumulada R$", "Variação Acumulada %"}


@pytest.mark.skipif(CSV is None, reason="CSV de exemplo não está disponível")
def test_unknown_rows_are_reported_and_untouched():
    grid = sheet_from_csv()
    ws = FakeWorksheet(grid)
    data = data_for(grid)
    link = ss.find_col(grid[0], "Link Direto")
    removed = grid[2][link]
    del data[removed]
    rep = ss.apply_to_sheet(ws, data, date(2026, 11, 16))
    assert rep.unmatched_sheet_rows == [grid[2][1]]
    assert ws.cell(2, "Status") == grid[2][ss.find_col(grid[0], "Status")]


def test_status_label():
    assert ss.status_label(D("90"), D("100"), None) == "Caiu"
    assert ss.status_label(D("110"), D("100"), None) == "Subiu"
    assert ss.status_label(D("100"), D("100"), None) == "Estável"
    assert ss.status_label(None, None, "erro") == "Falha na coleta"
    assert ss.status_label(D("100"), None, None) == "Estável"


def test_dry_run_writes_nothing():
    grid = [["Produto", "Preço Atual em 02/11/2026", "Status", "Variação Acumulada R$",
             "Variação Acumulada %", "Parcelado Total", "Parcelas", "Avaliação", "Qtd Avaliações",
             "Valor Inicial em 15/10/2026", "Link Direto"],
            ["A", 100.0, "Estável", "", "", "", "", "", "", 120.0, "http://a"]]
    ws = FakeWorksheet(grid)
    ss.apply_to_sheet(ws, {"http://a": {"price": D("90"), "prev": D("100"), "flag": None,
                                        "valor_inicial": None}}, date(2026, 11, 16), dry_run=True)
    assert ws.calls == [] and ws.grid == grid
