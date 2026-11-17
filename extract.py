"""Leitura de preço a partir do HTML de uma página de produto.

Ordem de confiança (do mais ao menos confiável):
  1. regra específica da loja (STORE_RULES, regex que você mesmo ajusta)
  2. JSON-LD (<script type="application/ld+json"> com @type Product)
  3. meta tags (product:price:amount, og:price:amount, itemprop=price)
  4. texto da página ("R$ 1.234,56 no Pix") — só como último recurso

Páginas de loja têm carrosséis e anúncios com outros produtos; por isso nada é lido do texto
solto da página quando JSON-LD/meta já trouxeram o preço.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# Ajustes por domínio. Exemplo (descomente e adapte depois do primeiro --dry-run):
#   "www.magazineluiza.com.br": {"pix_regex": r"R\$\s*([\d\.]+,\d{2})\s*no Pix"},
#   "www.mercadolivre.com.br":  {"pick": "min"},     # min (padrão) | max | first
STORE_RULES: dict[str, dict[str, str]] = {}

TWO = Decimal("0.01")
_PIX_RE = re.compile(r"R\$\s*([\d\.]+,\d{2})\s*(?:no|via|à vista no|a vista no)\s*pix", re.I)
_AVISTA_RE = re.compile(r"R\$\s*([\d\.]+,\d{2})\s*(?:à|a)\s*vista", re.I)
_INST_RE = re.compile(r"(\d{1,2})\s*x\s*(?:de\s*)?R\$\s*([\d\.]+,\d{2})", re.I)
_OUT_RE = re.compile(r"OutOfStock|SoldOut|Discontinued", re.I)
_IN_RE = re.compile(r"InStock|PreOrder|LimitedAvailability|OnlineOnly|InStoreOnly", re.I)


@dataclass
class Extracted:
    price: Optional[Decimal] = None            # preço à vista/Pix
    installment_total: Optional[Decimal] = None
    installments: Optional[int] = None
    rating: Optional[Decimal] = None
    reviews: Optional[int] = None
    available: Optional[bool] = None
    source: str = ""
    notes: list[str] = field(default_factory=list)


def parse_brl(text: Any) -> Optional[Decimal]:
    """'R$ 5.038,80' -> Decimal('5038.80'); '-R$ 124,58' -> Decimal('-124.58'); '2,41%' -> 2.41"""
    if text is None:
        return None
    if isinstance(text, (int, float, Decimal)):
        return Decimal(str(text))
    s = str(text).replace("R$", "").replace("%", "").replace("\xa0", "").replace(" ", "").strip()
    if not s:
        return None
    neg = s.startswith("-")
    s = s.lstrip("+-")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if neg else d


def _json_number(v: Any) -> Optional[Decimal]:
    """Números de JSON-LD usam ponto decimal ('4999.00'); aceita também formato BR com vírgula."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    s = str(v).strip()
    if "," in s:
        return parse_brl(s)
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _walk_ld(node: Any):
    if isinstance(node, list):
        for n in node:
            yield from _walk_ld(n)
    elif isinstance(node, dict):
        yield node
        if "@graph" in node:
            yield from _walk_ld(node["@graph"])


def _types(node: dict) -> list[str]:
    t = node.get("@type")
    return [str(x) for x in t] if isinstance(t, list) else [str(t)]


def _offers(off: Any) -> list[tuple[Decimal, Optional[str]]]:
    out: list[tuple[Decimal, Optional[str]]] = []
    if isinstance(off, list):
        for o in off:
            out += _offers(o)
    elif isinstance(off, dict):
        availability = off.get("availability")
        if "offers" in off:
            out += _offers(off["offers"])
        found = False
        for key in ("price", "lowPrice"):
            p = _json_number(off.get(key))
            if p is not None and p > 0:
                out.append((p, availability))
                found = True
                break
        if not found:
            specs = off.get("priceSpecification")
            for spec in (specs if isinstance(specs, list) else [specs] if specs else []):
                if not isinstance(spec, dict):
                    continue
                if re.search(r"List|Strikethrough", str(spec.get("priceType", "")), re.I):
                    continue
                p = _json_number(spec.get("price"))
                if p is not None and p > 0:
                    out.append((p, availability))
    return out


def _from_jsonld(soup: BeautifulSoup, rule: dict[str, str]) -> Extracted:
    res = Extracted()
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _walk_ld(data):
            if not any(t in ("Product", "ProductGroup") for t in _types(node)):
                continue
            offers = _offers(node.get("offers"))
            if not offers:
                # oferta sem preço (ex.: price "0" + OutOfStock): o produto está sem estoque
                raw_offers = json.dumps(node.get("offers") or "")
                if _OUT_RE.search(raw_offers) and not _IN_RE.search(raw_offers):
                    res.available = False
                    res.source = "jsonld"
                    return res
                continue
            in_stock = [p for p, a in offers if a and _IN_RE.search(str(a))]
            out_stock = [p for p, a in offers if a and _OUT_RE.search(str(a))]
            if in_stock:
                res.available = True
            elif out_stock:
                res.available = False
            candidates = in_stock or [p for p, _ in offers]
            pick = rule.get("pick", "min")
            res.price = {"max": max, "first": lambda c: c[0]}.get(pick, min)(candidates)
            res.source = "jsonld"
            if len(set(candidates)) > 1:
                vals = ", ".join(f"{p:.2f}" for p in sorted(set(candidates)))
                res.notes.append(f"várias ofertas no JSON-LD ({vals}); usei '{pick}'")
            agg = node.get("aggregateRating")
            if isinstance(agg, dict):
                rv = _json_number(agg.get("ratingValue"))
                if rv is not None:
                    res.rating = rv.quantize(Decimal("0.1"))
                cnt = agg.get("reviewCount", agg.get("ratingCount"))
                try:
                    res.reviews = int(str(cnt).replace(".", "")) if cnt is not None else None
                except ValueError:
                    pass
            return res
    return res


def _from_meta(soup: BeautifulSoup) -> Optional[Decimal]:
    for attrs in (
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            p = _json_number(tag["content"])
            if p is not None and p > 0:
                return p
    return None


def _installments(text: str, pix: Optional[Decimal]) -> Optional[tuple[int, Decimal]]:
    """Maior parcelamento cujo total fica perto do preço à vista (filtra ruído de carrossel)."""
    if pix is None:
        return None
    best: Optional[tuple[int, Decimal]] = None
    for m in _INST_RE.finditer(text):
        n = int(m.group(1))
        v = parse_brl(m.group(2))
        if n < 2 or v is None:
            continue
        total = (v * n).quantize(TWO)
        if not (pix * Decimal("0.95") <= total <= pix * Decimal("1.6")):
            continue
        if best is None or n > best[0]:
            best = (n, total)
    return best


def extract(html: str, url: str = "") -> Extracted:
    soup = BeautifulSoup(html, "html.parser")
    rule = STORE_RULES.get(urlparse(url).netloc, {})
    text = soup.get_text(" ", strip=True)

    res = _from_jsonld(soup, rule)

    if rule.get("pix_regex"):
        m = re.search(rule["pix_regex"], text, re.I)
        if m:
            res.price = parse_brl(m.group(1))
            res.source = "regra-loja"
    if res.price is None:
        meta = _from_meta(soup)
        if meta is not None:
            res.price, res.source = meta, "meta"
    if res.price is None:
        m = _PIX_RE.search(text) or _AVISTA_RE.search(text)
        if m:
            res.price, res.source = parse_brl(m.group(1)), "texto"
            res.notes.append("preço lido do texto da página (menos confiável)")

    inst = _installments(text, res.price)
    if inst:
        res.installments, res.installment_total = inst
    if res.available is None and re.search(r"produto indispon[ií]vel|esgotad[oa]", text, re.I) and res.price is None:
        res.available = False
    return res
