"""
CDP tabanlı scraper — botu senin GERÇEK, giriş yapmış Chrome'una bağlar.

Chrome'u bir kez uzaktan hata ayıklama portuyla başlatırsın (bkz. README / baslat.sh):
    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
        --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-bot-binance"

Bot bu çalışan tarayıcıya bağlanır; istekleri gerçek oturumun içinden yapar.
Böylece bellek-içi session cookie'leri (csrftoken, www p20t) ve gerçek cihaz
parmak izi kullanılır — Binance oturumu kabul eder.

Aynı arayüz: get_trader_state() -> {'balance', 'positions'}.
"""

from playwright.sync_api import sync_playwright

from .binance_scraper import ScraperError

_BASE = "https://www.binance.com/bapi/asset/v1"


class SessionExpired(ScraperError):
    """Chrome'da Binance oturumu kapanmış -> tarayıcıda yeniden giriş gerekir."""


def _profile_url(trader_id):
    return f"https://www.binance.com/en-TR/smart-money/profile/{trader_id}"


class CDPSession:
    _EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"  # boş csrf (giriş yapılmamış istekler)

    def __init__(self, cdp_url, trader_id):
        self.cdp_url = cdp_url
        self.trader_id = trader_id
        self._pw = self._browser = self._ctx = self._page = None
        self._csrf_token = ""
        self._owns_page = False  # sekmeyi biz mi açtık (kapanışta sadece onu kapat)

    def _on_request(self, req):
        # SPA'nın kendi bapi isteklerinden canlı csrftoken'ı yakala.
        c = req.headers.get("csrftoken")
        if c and "bapi" in req.url and c != self._EMPTY_MD5:
            self._csrf_token = c

    def start(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        if not self._browser.contexts:
            raise ScraperError("Chrome'a bağlanıldı ama açık context yok.")
        self._ctx = self._browser.contexts[0]
        # Açık bir Binance sekmesi varsa onu yeniden kullan (zaten girili oturum).
        # Önce tam profil sekmemizi, yoksa herhangi bir binance.com sekmesini seç.
        # Binance DIŞI sekmelere (Twitter vb.) asla dokunma/yönlendirme/kapatma.
        target = f"/smart-money/profile/{self.trader_id}"
        on_profile = [p for p in self._ctx.pages if target in (p.url or "")]
        any_binance = [p for p in self._ctx.pages if "binance.com" in (p.url or "")]
        if on_profile:
            self._page = on_profile[0]          # zaten profil sekmesi açık
        elif any_binance:
            self._page = any_binance[0]         # başka bir Binance sekmesi (girili)
        else:
            self._page = self._ctx.new_page()   # hiç Binance yok -> tek sekme aç
            self._owns_page = True
        self._page.on("request", self._on_request)
        self._warm()

    def _warm(self):
        """Profil sayfasını yükle: oturumu tazeler ve csrftoken'ı yakalar.

        Sekme zaten profil sayfasındaysa gereksiz yönlendirme yapma; aksi halde
        (yeni sekme veya başka bir Binance sayfası) profil sayfasına götür.
        """
        target = f"/smart-money/profile/{self.trader_id}"
        if target not in (self._page.url or ""):
            self._page.goto(_profile_url(self.trader_id),
                            wait_until="networkidle", timeout=60000)
        else:
            self._page.reload(wait_until="networkidle", timeout=60000)
        self._page.wait_for_timeout(3000)

    def close(self):
        try:
            if self._browser:
                self._browser.close()  # sadece CDP bağlantısını kapatır, Chrome açık kalır
        finally:
            if self._pw:
                self._pw.stop()

    def __enter__(self):
        self.start(); return self

    def __exit__(self, *exc):
        self.close()

    def is_logged_in(self):
        return any(c["name"] == "logined" and c["value"] == "y" for c in self._ctx.cookies())

    def _raw_get(self, url):
        resp = self._page.request.get(url, headers={
            "accept": "*/*", "clienttype": "web", "lang": "en-TR",
            "csrftoken": self._csrf_token, "referer": _profile_url(self.trader_id),
        }, timeout=20000)
        return resp

    def _api_get(self, url, _retry=True):
        resp = self._raw_get(url)
        if resp.status == 401 and _retry:
            # csrf bayatlamış olabilir: sayfayı tazeleyip bir kez daha dene.
            self._warm()
            return self._api_get(url, _retry=False)
        if resp.status == 401:
            raise SessionExpired("401: Chrome'daki Binance oturumu kapanmış, yeniden giriş yapın.")
        try:
            data = resp.json()
        except Exception:
            raise ScraperError(f"JSON parse hatası (status {resp.status}).")
        if not data.get("success"):
            code = str(data.get("code"))
            if code.startswith("10000200") or code == "100001005":
                if _retry:
                    self._warm()
                    return self._api_get(url, _retry=False)
                raise SessionExpired(f"Oturum geçersiz: {data.get('message')}")
            raise ScraperError(f"API başarısız: code={code} msg={data.get('message')}")
        return data

    def fetch_positions(self, market_type="UM"):
        url = (f"{_BASE}/private/future/smart-money/profile/query-positions"
               f"?topTraderId={self.trader_id}&marketType={market_type}&page=1&rows=50")
        out = []
        for item in self._api_get(url).get("data") or []:
            amt = float(item["amount"])
            if amt == 0:
                continue
            out.append({
                "symbol": item["symbol"],
                "side": "LONG" if amt > 0 else "SHORT",
                "size": abs(amt),
                "leverage": int(item.get("leverage") or 0),
                "margin": float(item.get("margin") or 0.0),
                "entry_price": float(item.get("entryPrice") or 0.0),
                "market_type": market_type,
            })
        return out

    def fetch_trader_balance(self):
        url = (f"{_BASE}/friendly/future/smart-money/profile/chart-data"
               f"?topTraderId={self.trader_id}&timeRange=30D&chartDataType=BALANCE")
        items = (self._api_get(url).get("data") or {}).get("items") or []
        if not items:
            raise ScraperError("Bakiye serisi boş döndü.")
        return float(items[-1][1])

    def get_trader_state(self, include_coin_m=False):
        positions = self.fetch_positions("UM")
        if include_coin_m:
            positions += self.fetch_positions("CM")
        return {"balance": self.fetch_trader_balance(), "positions": positions}
