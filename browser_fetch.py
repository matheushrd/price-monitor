"""Baixa páginas com um Chrome/Chromium de verdade (Playwright), para lojas que só mostram o preço via
JavaScript ou que bloqueiam clientes HTTP simples (Magalu, Casas Bahia, Mercado Livre, Daikin...).

No Debian do celular: use o Chromium do apt e uma tela virtual:  xvfb-run -a python collect.py --browser ...
Variável opcional BROWSER_PATH aponta para o executável. O perfil fica em ./browser-profile (cookies persistem).
"""
import os
import shutil
import sys
from pathlib import Path

PROFILE_DIR = Path(__file__).resolve().parent / "browser-profile"
_CHALLENGE_MARKERS = ("sec-if-cpt", "sec-cpt", "akamai", "verifying your browser", "just a moment",
                      "um momento", "verificando", "cf-challenge", "captcha", "access denied")


def _is_challenge(html: str) -> bool:
    """Páginas de desafio anti-robô são pequenas e citam o fornecedor da proteção."""
    low = html.lower()
    return len(html) < 15000 and any(m in low for m in _CHALLENGE_MARKERS)


def _find_browser() -> str | None:
    env = os.environ.get("BROWSER_PATH")
    if env:
        return env
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    return None


class BrowserFetcher:
    def __init__(self, headless: bool = False):
        from playwright.sync_api import sync_playwright  # type: ignore

        self._pw = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled"]
        if sys.platform.startswith("linux"):
            # proot/Termux não tem sandbox de kernel nem /dev/shm decente. Além disso o Android mata processos
            # quando há muitos filhos (e o PostgreSQL do Termux vai junto): poucos processos e pouca memória.
            args += [
                "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--no-zygote", "--renderer-process-limit=1",
                "--disable-features=site-per-process,IsolateOrigins,Translate",
                "--disable-extensions", "--disable-background-networking", "--disable-sync",
                "--js-flags=--max-old-space-size=256",
            ]
        opts = dict(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1366, "height": 900},
            args=args,
        )
        exe = _find_browser()
        if exe:
            opts["executable_path"] = exe
        self.ctx = self._pw.chromium.launch_persistent_context(**opts)
        self.ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        if os.environ.get("BLOCK_ASSETS", "1") == "1":
            # imagens, vídeos e fontes não trazem preço e pesam na memória do celular
            self.ctx.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("image", "media", "font")
                else route.continue_(),
            )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def get(self, url: str, timeout_ms: int = 60000) -> tuple[int, str]:
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        # Desafios (Akamai/Cloudflare) rodam alguns segundos e recarregam a página sozinhos.
        # Espera até 50 s, mexendo o mouse como uma pessoa, e tenta recarregar uma vez no meio.
        html = ""
        for i in range(25):
            self.page.wait_for_timeout(2000)
            try:
                self.page.mouse.move(200 + (i * 37) % 700, 200 + (i * 53) % 400, steps=8)
                html = self.page.content()
            except Exception:  # a página navegou no meio da leitura
                continue
            if not _is_challenge(html):
                break
            if i == 12:
                try:
                    self.page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception:
                    pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        self.page.mouse.wheel(0, 700)  # dispara carregamento "preguiçoso" de preços/avaliações
        self.page.wait_for_timeout(1200)
        return (resp.status if resp else 0), self.page.content()

    def close(self) -> None:
        try:
            self.ctx.close()
        finally:
            self._pw.stop()
