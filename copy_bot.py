"""
Ana bot: Binance smart-money trader'ını oransal kopyalar.

Akış (her döngü):
  1) Scraper ile trader durumunu çek (bakiye + pozisyonlar)   [salt-okunur web]
  2) Kendi hesabımızın bakiye + pozisyonlarını çek            [binance api]
  3) Motor ile emir niyetlerini hesapla                        [saf mantık]
  4) DRY_RUN ise sadece logla; değilse emirleri gönder

GÜVENLİK: DRY_RUN=True iken HİÇBİR emir gönderilmez. Canlıya geçmeden önce
testnet'te DRY_RUN=False ile davranışı doğrula.
"""

import time
from datetime import datetime

from binance.client import Client

import config
from scraper.binance_scraper import ScraperError
from scraper.cdp_session import CDPSession, SessionExpired
from engine.sync_engine import plan_orders
from engine.tracker import CopyTracker


LOG_FILE = "bot.log"

# Atlanacak semboller (config'den seed; -4411 hatasında çalışma anında genişler).
SKIP_SYMBOLS = set(getattr(config, "SKIP_SYMBOLS", []))


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


class Executor:
    """Binance Futures sarmalayıcısı: hesap durumu + emir + filtre cache."""

    def __init__(self, client):
        self.client = client
        self._filters = {}      # symbol -> step_size (bir kez çekilir)
        self._leverage = {}     # symbol -> son ayarlanan kaldıraç
        self._margin_set = {}   # symbol -> cross margin ayarlandı mı

    def get_balance(self):
        info = self.client.futures_account()
        return float(info["totalMarginBalance"])

    def account_snapshot(self):
        """TEK futures_account çağrısıyla bakiye + pozisyonlar (rate-limit dostu)."""
        info = self.client.futures_account()
        balance = float(info["totalMarginBalance"])
        # Ölçek paydası için PnL'siz, tik-tik titremeyen bakiye (whipsaw önler).
        # totalMarginBalance gerçekleşmemiş PnL içerir -> her mark hareketinde scale oynar.
        self.last_wallet_balance = float(info.get("totalWalletBalance") or balance)
        out = {}
        for a in info["positions"]:
            amt = float(a.get("positionAmt") or 0)
            if amt == 0:
                continue
            margin = float(a.get("positionInitialMargin") or a.get("initialMargin") or 0.0)
            maint = float(a.get("maintMargin") or 0.0)
            notional = abs(float(a.get("notional") or 0.0))
            out[a["symbol"]] = {
                "side": "LONG" if amt > 0 else "SHORT", "size": abs(amt),
                "leverage": int(float(a.get("leverage") or 0)),
                "entry_price": float(a.get("entryPrice") or 0.0),
                "mark_price": (notional / abs(amt)) if amt else 0.0,
                "pnl": float(a.get("unrealizedProfit") or a.get("unRealizedProfit") or 0.0),
                "margin": margin,
                "margin_ratio": round(maint / notional * 100, 2) if notional else None,
                "isolated": bool(a.get("isolated")),
                "liq_price": 0.0,
            }
        # liq fiyatı futures_account'ta yok -> position_information'dan ekle (tek ek çağrı)
        if out:
            try:
                for p in self.client.futures_position_information():
                    s = p["symbol"]
                    if s in out:
                        out[s]["liq_price"] = float(p.get("liquidationPrice") or 0.0)
            except Exception:
                pass
        return balance, out

    def mark_prices(self):
        """TEK çağrıyla tüm sembollerin mark fiyatı: {symbol: float}."""
        out = {}
        for m in self.client.futures_mark_price():
            try:
                out[m["symbol"]] = float(m["markPrice"])
            except (KeyError, TypeError, ValueError):
                pass
        return out

    def get_positions(self):
        """{symbol: {...}} — sadece açık (size>0) pozisyonlar, zengin alanlarla.

        Kaldıraç ve gerçek margin position_information'da gelmiyor; futures_account'tan
        alınıp birleştirilir.
        """
        acc = {p["symbol"]: p for p in self.client.futures_account()["positions"]}
        out = {}
        for p in self.client.futures_position_information():
            amt = float(p["positionAmt"])
            if amt == 0:
                continue
            a = acc.get(p["symbol"], {})
            margin = float(a.get("positionInitialMargin") or a.get("initialMargin") or 0.0)
            maint = float(a.get("maintMargin") or 0.0)
            notional = abs(float(a.get("notional") or 0.0))
            out[p["symbol"]] = {
                "side": "LONG" if amt > 0 else "SHORT",
                "size": abs(amt),
                "leverage": int(float(a.get("leverage") or p.get("leverage") or 0)),
                "entry_price": float(p.get("entryPrice") or 0.0),
                "mark_price": float(p.get("markPrice") or 0.0),
                "liq_price": float(p.get("liquidationPrice") or 0.0),
                "pnl": float(p.get("unRealizedProfit") or 0.0),
                "margin": margin,
                "margin_ratio": round(maint / notional * 100, 2) if notional else None,
                "isolated": bool(a.get("isolated")),
            }
        return out

    def _load_filters(self):
        info = self.client.futures_exchange_info()
        for item in info["symbols"]:
            for f in item["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    self._filters[item["symbol"]] = float(f["stepSize"])

    def step_size(self, symbol):
        if not self._filters:
            self._load_filters()
        return self._filters.get(symbol, 0.001)

    def mark_price(self, symbol):
        try:
            return float(self.client.futures_mark_price(symbol=symbol)["markPrice"])
        except Exception:
            return 0.0

    def ensure_margin_type(self, symbol):
        """Sembolü CROSS margin'e ayarlar (likidasyonun trader ile eşleşmesi için)."""
        if self._margin_set.get(symbol):
            return
        try:
            self.client.futures_change_margin_type(symbol=symbol, marginType="CROSSED")
        except Exception as e:
            # -4046: zaten cross (sorun değil); pozisyon açıkken değiştirilemez (uyar)
            if "-4046" not in str(e):
                log(f"⚠️ Margin tipi CROSS yapılamadı {symbol}: {e}")
        self._margin_set[symbol] = True

    def ensure_leverage(self, symbol, leverage):
        if not leverage or self._leverage.get(symbol) == leverage:
            return
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            self._leverage[symbol] = leverage
        except Exception as e:
            log(f"⚠️ Kaldıraç ayarlanamadı {symbol} {leverage}x: {e}")

    def market_order(self, symbol, side, qty, reduce_only):
        return self.client.futures_create_order(
            symbol=symbol, side=side, type="MARKET",
            quantity=qty, reduceOnly=reduce_only,
        )


def build_market_data(execu, trader_state, my_positions, price_map=None):
    """Motorun ihtiyaç duyduğu sembol verisini (step_size + mark price) toplar.

    price_map verilirse fiyatlar oradan okunur (sembol başına API çağrısı yapılmaz).
    """
    symbols = {p["symbol"] for p in trader_state["positions"]} | set(my_positions)
    md = {}
    for s in symbols:
        price = price_map.get(s) if price_map is not None else execu.mark_price(s)
        md[s] = {"step_size": execu.step_size(s), "price": price or 0.0}
    return md


def run_once(execu, session, tracker):
    trader_state = session.get_trader_state(include_coin_m=config.COPY_COIN_M)

    # İlk döngü: mevcut trader pozisyonlarını baseline al, görmezden gel.
    if not tracker.initialized:
        tracker.initialize(trader_state["positions"])
        log(f"📌 Baseline kaydedildi: {len(trader_state['positions'])} mevcut pozisyon "
            f"görmezden gelinecek. Bundan sonraki değişiklikler takip edilecek.")
        return

    # Trader'ın bottan sonraki değişiklikleri (baseline'a göre kopyalanabilir kısım).
    eff_positions = tracker.effective_positions(trader_state["positions"])
    # Atlanacak sembolleri (TradFi-Perps vb.) çıkar.
    eff_positions = [p for p in eff_positions if p["symbol"] not in SKIP_SYMBOLS]
    eff_state = {"balance": trader_state["balance"], "positions": eff_positions}

    my_balance = execu.get_balance()
    my_all = execu.get_positions()
    # Yalnızca bot'un yönettiği sembolleri motora ver (diğerlerine dokunma).
    my_managed = tracker.select_managed_positions(eff_positions, my_all)
    market_data = build_market_data(execu, eff_state, my_managed)

    orders, scale = plan_orders(
        eff_state, my_balance, my_managed, market_data,
        tolerance=config.REBALANCE_TOLERANCE, min_notional=config.MIN_NOTIONAL_USDT,
        size_multiplier=getattr(config, "SIZE_MULTIPLIER", 1.0),
    )

    tracker.update_managed(eff_positions, my_managed)

    log(f"trader_bal={trader_state['balance']:.0f} my_bal={my_balance:.2f} "
        f"scale={scale:.5f} | takip_edilen={len(eff_positions)} "
        f"yönetilen={len(my_managed)} | {len(orders)} emir")

    if not orders:
        log("✅ Senkron: değişiklik yok.")
        return

    for o in orders:
        tag = "[DRY]" if config.DRY_RUN else "[LIVE]"
        line = (f"  {tag} {o['side']} {o['qty']} {o['symbol']} "
                f"reduce={o['reduce_only']} | {o['reason']}")
        log(line)
        if config.DRY_RUN:
            continue
        try:
            execu.ensure_leverage(o["symbol"], o.get("leverage"))
            res = execu.market_order(o["symbol"], o["side"], o["qty"], o["reduce_only"])
            log(f"     -> OK orderId={res.get('orderId')}")
        except Exception as e:
            msg = str(e)
            log(f"     -> ❌ Emir hatası: {msg}")
            if "-4411" in msg:
                SKIP_SYMBOLS.add(o["symbol"])
                log(f"     -> ⏭️  {o['symbol']} TradFi-Perps sözleşmesi istiyor; "
                    f"bundan sonra atlanacak.")


def main():
    print("=" * 60)
    print(f"🤖 Copy-Trade Bot | TESTNET={config.USE_TESTNET} | DRY_RUN={config.DRY_RUN}")
    print("=" * 60)

    client = Client(config.BINANCE_API_KEY, config.BINANCE_API_SECRET,
                    testnet=config.USE_TESTNET)
    execu = Executor(client)

    log("🌐 Chrome'a (CDP) bağlanılıyor...")
    try:
        session = CDPSession(config.CDP_URL, config.TOP_TRADER_ID)
        session.start()
    except Exception as e:
        log(f"❌ Chrome'a bağlanılamadı: {e}")
        log("   Chrome'u şu komutla başlat (veya ./baslat.sh kullan):")
        log('   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" '
            '--remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-bot-binance"')
        return
    if not session.is_logged_in():
        log("❌ Chrome'da Binance oturumu yok. Tarayıcıda giriş yapın.")
        session.close()
        return
    log("✅ Oturum aktif.")

    tracker = CopyTracker()
    try:
        while True:
            try:
                run_once(execu, session, tracker)
            except SessionExpired as e:
                log(f"🔒 Oturum sona erdi (emir gönderilmedi): {e}")
            except ScraperError as e:
                log(f"🕸️ Scraper hatası (emir gönderilmedi): {e}")
            except Exception as e:
                log(f"🚨 Beklenmeyen hata: {e}")
            time.sleep(config.POLL_INTERVAL_SEC)
    except KeyboardInterrupt:
        log("👋 Operatör isteğiyle durduruldu.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
