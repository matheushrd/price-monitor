import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract as ex  # noqa: E402
from extract import extract, parse_brl  # noqa: E402


def page(ld="", body="", head=""):
    return f"<html><head>{head}<script type='application/ld+json'>{ld}</script></head><body>{body}</body></html>"


def test_parse_brl():
    assert parse_brl("R$ 5.038,80") == Decimal("5038.80")
    assert parse_brl("R$5.163,38") == Decimal("5163.38")
    assert parse_brl("-R$ 124,58") == Decimal("-124.58")
    assert parse_brl("-2,41%") == Decimal("-2.41")
    assert parse_brl("5,0") == Decimal("5.0")
    assert parse_brl("1.200") == Decimal("1200")
    assert parse_brl("") is None and parse_brl(None) is None and parse_brl("abc") is None


def test_jsonld_single_offer_and_rating():
    ld = """{"@type":"Product","name":"X","aggregateRating":{"ratingValue":"4.8","reviewCount":187},
             "offers":{"@type":"Offer","price":"2399.00","availability":"https://schema.org/InStock"}}"""
    r = extract(page(ld))
    assert r.price == Decimal("2399.00") and r.source == "jsonld"
    assert r.available is True
    assert r.rating == Decimal("4.8") and r.reviews == 187


def test_jsonld_graph_and_aggregate_offer_uses_low_price():
    ld = """{"@graph":[{"@type":"BreadcrumbList"},{"@type":["Product"],"offers":
            {"@type":"AggregateOffer","lowPrice":3919.0,"highPrice":4354.44}}]}"""
    r = extract(page(ld))
    assert r.price == Decimal("3919.0")


def test_jsonld_multiple_offers_takes_min_and_notes_it():
    ld = """{"@type":"Product","offers":[{"price":"1000.00","availability":"InStock"},
            {"price":"900.00","availability":"InStock"}]}"""
    r = extract(page(ld))
    assert r.price == Decimal("900.00")
    assert any("várias ofertas" in n for n in r.notes)


def test_jsonld_ignores_list_price_in_price_specification():
    ld = """{"@type":"Product","offers":{"availability":"InStock","priceSpecification":[
            {"price":"2000.00","priceType":"https://schema.org/ListPrice"},
            {"price":"1500.00","priceType":"https://schema.org/SalePrice"}]}}"""
    assert extract(page(ld)).price == Decimal("1500.00")


def test_out_of_stock():
    ld = """{"@type":"Product","offers":{"price":"599.00","availability":"https://schema.org/OutOfStock"}}"""
    r = extract(page(ld))
    assert r.available is False


def test_meta_fallback():
    r = extract(page(head='<meta property="product:price:amount" content="1234.56">'))
    assert r.price == Decimal("1234.56") and r.source == "meta"


def test_text_fallback_pix():
    r = extract(page(body="<p>Por R$ 1.101,60 no Pix</p><p>ou 10x de R$ 122,40 sem juros</p>"))
    assert r.price == Decimal("1101.60") and r.source == "texto"
    assert r.installments == 10 and r.installment_total == Decimal("1224.00")


def test_installments_ignore_carousel_noise():
    ld = """{"@type":"Product","offers":{"price":"5038.80","availability":"InStock"}}"""
    body = ("<p>10x de R$ 530,40 sem juros</p>"
            "<div class='carrossel'>12x de R$ 50,00 | 24x de R$ 4,00</div>")
    r = extract(page(ld, body))
    assert (r.installments, r.installment_total) == (10, Decimal("5304.00"))


def test_installments_need_a_price():
    assert extract(page(body="10x de R$ 10,00")).installments is None


def test_store_rule_regex_overrides(monkeypatch):
    monkeypatch.setitem(ex.STORE_RULES, "loja.exemplo.com", {"pix_regex": r"Pix:\s*R\$\s*([\d\.]+,\d{2})"})
    ld = """{"@type":"Product","offers":{"price":"999.00"}}"""
    r = extract(page(ld, "<span>Pix: R$ 850,00</span>"), "https://loja.exemplo.com/p/1")
    assert r.price == Decimal("850.00") and r.source == "regra-loja"


def test_pick_max_rule(monkeypatch):
    monkeypatch.setitem(ex.STORE_RULES, "loja.exemplo.com", {"pick": "max"})
    ld = """{"@type":"Product","offers":[{"price":"100"},{"price":"120"}]}"""
    assert extract(page(ld), "https://loja.exemplo.com/p").price == Decimal("120")


def test_broken_or_empty_html():
    r = extract("<html><script type='application/ld+json'>{nope</script></html>")
    assert r.price is None and r.available is None
    assert extract("").price is None


def test_classify():
    import collect

    D = Decimal
    assert collect.classify(D("100"), D("100"), None) == "ok"
    assert collect.classify(D("70"), D("100"), None) == "ok"           # -30% ainda aceito
    assert collect.classify(D("50"), D("100"), None) == "suspeito"     # -50%
    assert collect.classify(D("400"), D("100"), None) == "suspeito"    # +300%
    assert collect.classify(None, D("100"), None) == "erro"
    assert collect.classify(D("100"), D("100"), False) == "indisponivel"
    assert collect.classify(D("999"), None, None) == "ok"              # primeiro preço


def test_jsonld_sem_estoque_preco_zero():
    html = ('<html><script type="application/ld+json">{"@type":"Product","name":"X","offers":'
            '{"@type":"Offer","price":"0","priceCurrency":"BRL",'
            '"availability":"https://schema.org/OutOfStock"}}</script></html>')
    r = extract(html, "https://www.leroymerlin.com.br/x")
    assert r.price is None and r.available is False
