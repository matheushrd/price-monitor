"""Coleta os preços de hoje e grava no PostgreSQL.

    python collect.py                 # coleta todos os produtos ativos
    python collect.py --dry-run       # mostra o que leria, sem gravar (use no primeiro teste)
    python collect.py --only 3,7      # só esses IDs
    python collect.py --approve 7     # aceita o preço 'suspeito' de hoje do produto 7
"""
from __future__ import annotations

import argparse
import random
import time
from decimal import Decimal
from typing import Optional

import config
import db
from extract import Extracted, extract

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
}


PROFILES = ("chrome", "chrome131_android", "safari17_0")


def fetch(url: str, timeout: int = 30) -> tuple[int, str]:
    """curl_cffi imita o TLS de navegadores reais; tenta vários perfis se a loja bloquear (403/429/503)."""
    try:
        from curl_cffi import requests as cr  # type: ignore
    except ImportError:
        import requests

        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        return r.status_code, r.text

    code, text = 0, ""
    for prof in PROFILES:
        r = cr.get(url, headers={"Accept-Language": HEADERS["Accept-Language"]},
                   impersonate=prof, timeout=timeout, allow_redirects=True)
        code, text = r.status_code, r.text
        if code not in (403, 429, 503):
            break
    return code, text


def classify(price: Optional[Decimal], last_ok: Optional[Decimal], available: Optional[bool]) -> str:
    if available is False:
        return "indisponivel"
    if price is None:
        return "erro"
    if last_ok and last_ok > 0 and abs(price - last_ok) / last_ok > config.MAX_JUMP:
        return "suspeito"
    return "ok"


def _last_ok(cur, pid: int, today) -> Optional[Decimal]:
    cur.execute(
        """SELECT preco_pix FROM price_snapshots
           WHERE product_id=%s AND status='ok' AND preco_pix IS NOT NULL AND data < %s
           ORDER BY data DESC LIMIT 1""",
        (pid, today),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _save(cur, pid: int, today, ex: Extracted, status: str, detail: str) -> None:
    cur.execute(
        """INSERT INTO price_snapshots
             (product_id, data, preco_pix, parcelado_total, parcelas, avaliacao, qtd_avaliacoes,
              status, origem, fonte_preco, detalhe, coletado_em)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'coletor',%s,%s, now())
           ON CONFLICT (product_id, data) DO UPDATE SET
              preco_pix=EXCLUDED.preco_pix, parcelado_total=EXCLUDED.parcelado_total,
              parcelas=EXCLUDED.parcelas, avaliacao=EXCLUDED.avaliacao,
              qtd_avaliacoes=EXCLUDED.qtd_avaliacoes, status=EXCLUDED.status, origem='coletor',
              fonte_preco=EXCLUDED.fonte_preco, detalhe=EXCLUDED.detalhe, coletado_em=now()""",
        (pid, today, ex.price, ex.installment_total, ex.installments, ex.rating, ex.reviews,
         status, ex.source or None, detail or None),
    )


def run(dry_run: bool, only: Optional[set[int]], browser: bool = False, pending: bool = False,
        headless: bool = False) -> int:
    today = config.today()
    conn = db.connect()
    cur = conn.cursor()
    if pending:
        # só o que ainda não tem preço "resolvido" hoje (ok / indisponível / suspeito aguardando aprovação)
        cur.execute(
            """SELECT id, nome, loja, url FROM products p WHERE ativo AND NOT EXISTS (
                   SELECT 1 FROM price_snapshots s WHERE s.product_id = p.id AND s.data = %s
                     AND s.status IN ('ok','indisponivel','suspeito')) ORDER BY id""",
            (today,),
        )
    else:
        cur.execute("SELECT id, nome, loja, url FROM products WHERE ativo ORDER BY id")
    products = [p for p in cur.fetchall() if not only or p[0] in only]
    print(f"Coletando {len(products)} produtos em {today:%d/%m/%Y}" + (" (dry-run)" if dry_run else ""))

    fetcher = None
    if browser and products:
        from browser_fetch import BrowserFetcher

        fetcher = BrowserFetcher(headless=headless)
    get = fetcher.get if fetcher else fetch

    counts: dict[str, int] = {}
    for n, (pid, nome, loja, url) in enumerate(products):
        detail = ""
        ex = Extracted()
        try:
            code, html = get(url)
            if code != 200:
                detail = f"HTTP {code}"
            else:
                ex = extract(html, url)
                if ex.price is None and ex.available is not False:
                    detail = "preço não encontrado na página (layout mudou ou bloqueio)"
        except Exception as e:  # rede, timeout, TLS…
            detail = f"{type(e).__name__}: {e}"[:300]

        last = _last_ok(cur, pid, today)
        status = classify(ex.price, last, ex.available)
        if status == "suspeito":
            detail = f"variação grande: último preço bom R$ {last:.2f} -> R$ {ex.price:.2f}"
        if ex.notes:
            detail = "; ".join(filter(None, [detail, *ex.notes]))[:500]
        counts[status] = counts.get(status, 0) + 1

        price_txt = f"R$ {ex.price:.2f}" if ex.price is not None else "—"
        last_txt = f"R$ {last:.2f}" if last is not None else "—"
        print(f"[{pid:>3}] {status:<12} {price_txt:>12} (antes {last_txt:>12}) {loja or '':<15} {nome[:45]}"
              + (f"\n      -> {detail}" if detail else ""))

        if not dry_run:
            _save(cur, pid, today, ex, status, detail)
            conn.commit()
        if n < len(products) - 1:
            time.sleep(random.uniform(config.DELAY_MIN, config.DELAY_MAX))

    if fetcher:
        fetcher.close()
    conn.close()
    resumo = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    print(f"Resumo: {resumo}")
    return 0


def approve(pid: int) -> int:
    today = config.today()
    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE price_snapshots SET status='ok', detalhe=COALESCE(detalhe,'') || ' [aprovado manualmente]'
               WHERE product_id=%s AND data=%s AND status='suspeito'""",
            (pid, today),
        )
        n = cur.rowcount
    conn.commit()
    conn.close()
    print("Aprovado." if n else "Nada para aprovar (não há preço suspeito de hoje para esse ID).")
    print("Rode `python sheet_sync.py` para atualizar a planilha.")
    return 0 if n else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="IDs separados por vírgula")
    ap.add_argument("--approve", type=int, metavar="ID")
    ap.add_argument("--browser", action="store_true", help="usa um Chrome real (Playwright); para rodar no PC")
    ap.add_argument("--pending", action="store_true", help="só produtos sem preço resolvido hoje")
    ap.add_argument("--headless", action="store_true", help="com --browser: sem janela (mais fácil de ser bloqueado)")
    ap.add_argument("--save-html", type=int, metavar="ID", help="baixa a página do produto para diagnóstico")
    args = ap.parse_args()
    if args.approve:
        raise SystemExit(approve(args.approve))
    if args.save_html:
        conn = db.connect()
        cur = conn.cursor()
        cur.execute("SELECT url FROM products WHERE id=%s", (args.save_html,))
        url = cur.fetchone()[0]
        if args.browser:
            from browser_fetch import BrowserFetcher

            bf = BrowserFetcher(headless=args.headless)
            code, html = bf.get(url)
            bf.close()
        else:
            code, html = fetch(url)
        out = f"debug_{args.save_html}.html"
        open(out, "w", encoding="utf-8").write(html)
        print(f"HTTP {code}, {len(html)} bytes -> {out}")
        ex = extract(html, url)
        print(f"preço={ex.price} disponível={ex.available} fonte={ex.source} notas={ex.notes}")
        raise SystemExit(0)
    ids = {int(x) for x in args.only.split(",")} if args.only else None
    raise SystemExit(run(args.dry_run, ids, args.browser, args.pending, args.headless))
