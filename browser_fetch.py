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
_CHALLENGE_TITLES = ("verifying", "just a moment", "um momento", "verificando", "attention required", "access denied")


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
            # proot/Termux não tem sandbox de kernel nem /dev/shm decente
            args += ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
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
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def get(self, url: str, timeout_ms: int = 60000) -> tuple[int, str]:
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        # Espera um eventual desafio anti-robô ("Verificando seu navegador...") terminar.
        for _ in range(15):
            self.page.wait_for_timeout(1500)
            title = (self.page.title() or "").lower()
            if not any(k in title for k in _CHALLENGE_TITLES):
                break
        try:
            self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        self.page.mouse.wheel(0, 700)  # dispara carregamento "preguiçoso" de preços/avaliações
        self.page.wait_for_timeout(800)
        return (resp.status if resp else 0), self.page.content()

    def close(self) -> None:
        try:
            self.ctx.close()
        finally:
            self._pw.stop()
