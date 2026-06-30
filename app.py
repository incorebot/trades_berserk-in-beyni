"""
Localhost arayüzü + bot motoru (tek süreç).

Veri kaynağı: tarayıcı eklentisi (extension/). Eklenti, senin gerçek ve girili
Binance Chrome'unda çalışıp trader verisini buraya (/ingest) gönderir. Böylece
CDP / ayrı Chrome / yeniden giriş gerekmez ve Chrome sürümünden bağımsızdır.

Çalıştır:  python3 app.py   ->  http://127.0.0.1:8777
  1) Chrome'da bu klasördeki extension/ -> chrome://extensions -> "Load unpacked"
  2) Binance smart-money profil sayfası açık ve girili olsun
  3) Arayüzden pozisyonları "Takip Et"
"""

import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime

import os
from flask import Flask, jsonify, request, send_file
from binance.client import Client

import config
from engine.sync_engine import plan_orders, round_step, compute_scale_factor
from engine.tracker import CopyTracker
from copy_bot import Executor, build_market_data, SKIP_SYMBOLS

# ---------------------------------------------------------------- paylaşılan durum
LOCK = threading.RLock()  # yeniden girilebilir: log() kilit içindeyken de çağrılabilsin
TRACKER = CopyTracker(state_file=getattr(config, "FOLLOW_STATE_FILE", None))
INGESTED = {"ts": 0.0, "balance": 0.0, "positions": []}  # eklentiden gelen son veri
BRIDGE = {"ts": 0.0, "reason": ""}  # eklentinin son bildirdiği köprü durumu (neden durduğu vb.)
_RAW_TRADER = []  # follow() için son ham pozisyonlar
EXECU = None      # panik kapatma için Executor referansı
RUNTIME = {
    "status": "başlatılıyor", "dry_run": config.DRY_RUN, "testnet": config.USE_TESTNET,
    "scale": 0.0, "trader_balance": 0.0, "my_balance": 0.0,
    "trader_positions": [], "my_positions": [], "log": [], "updated": "",
}


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with LOCK:
        RUNTIME["log"].append(line)
        del RUNTIME["log"][:-200]


_TG_TOKEN = getattr(config, "TELEGRAM_TOKEN", "")
_TG_CHAT = getattr(config, "TELEGRAM_CHAT_ID", "")
_alert_ts = {}


def _tg_send(text):
    try:
        url = f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": _TG_CHAT, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=8)
    except Exception:
        pass


def notify(text, key=None, min_gap=0):
    """Telegram bildirimi (engellemez; ayrı iş parçacığı). key+min_gap ile tekrar baskısı."""
    if key and min_gap:
        now = time.time()
        if now - _alert_ts.get(key, 0) < min_gap:
            return
        _alert_ts[key] = now
    if not (_TG_TOKEN and _TG_CHAT):
        return
    threading.Thread(target=_tg_send, args=(text,), daemon=True).start()


def _enrich_mine(symbol, v):
    """Kendi pozisyonumuzu kart için biçimler (margin/margin_ratio account'tan gelir)."""
    margin = v.get("margin", 0.0)
    pnl = v.get("pnl", 0.0)
    roi = (pnl / margin) if margin else 0.0
    return {
        "symbol": symbol, "side": v.get("side"), "size": v.get("size", 0.0),
        "leverage": v.get("leverage", 0), "isolated": v.get("isolated", False),
        "entry_price": v.get("entry_price", 0.0), "mark_price": v.get("mark_price", 0.0),
        "liq_price": v.get("liq_price", 0.0),
        "margin": round(margin, 2), "roi": roi,
        "margin_ratio": v.get("margin_ratio"),
        "pnl": round(pnl, 2),
    }


def _normalize(raw_positions):
    """Eklenti ham verisini motorun beklediği biçime çevirir."""
    out = []
    for it in raw_positions:
        sym = it.get("symbol")
        try:
            amt = float(it["amount"])
        except (KeyError, TypeError, ValueError):
            continue
        if not sym or amt == 0:
            continue
        out.append({
            "symbol": sym,
            "side": "LONG" if amt > 0 else "SHORT",
            "size": abs(amt),
            "leverage": int(it.get("leverage") or 0),
            "margin": float(it.get("margin") or 0.0),
            "isolated": bool(it.get("isolated")),
            "entry_price": float(it.get("entryPrice") or 0.0),
            "mark_price": float(it.get("markPrice") or 0.0),
            "liq_price": float(it.get("liqPrice") or 0.0),
            "margin_ratio": float(it.get("marginRatio") or 0.0),
            "pnl": float(it.get("pnl") or 0.0),
            "roi": float(it.get("roi") or 0.0),
        })
    return out


# ---------------------------------------------------------------- bot döngüsü
def bot_loop():
    try:
        client = Client(config.BINANCE_API_KEY, config.BINANCE_API_SECRET,
                        testnet=config.USE_TESTNET)
        execu = Executor(client)
        global EXECU
        EXECU = execu
    except Exception as e:
        log(f"❌ Binance bağlantı hatası: {e}")
        with LOCK: RUNTIME["status"] = "binance hatası"
        return

    log("🤖 Bot hazır. Tarayıcı eklentisinden veri bekleniyor...")
    notify(f"🟢 Bot başladı · MOD {'CANLI' if not config.DRY_RUN else 'TEST'}")
    global _RAW_TRADER
    data_ok = True  # veri akışı durumu (geçiş bildirimi için)
    while True:
        try:
            with LOCK:
                fresh = (time.time() - INGESTED["ts"]) < 90  # arka plan çekimi ~30sn; pay bırak
                raw = list(INGESTED["positions"])
                balance = INGESTED["balance"]
            if not fresh:
                with LOCK:
                    br = BRIDGE["reason"] if (time.time() - BRIDGE["ts"]) < 120 else ""
                    RUNTIME["status"] = (
                        f"veri bekleniyor — {br}" if br
                        else "veri bekleniyor (eklenti + Binance sekmesi açık mı?)")
                if data_ok:
                    data_ok = False
                    notify("⚠️ Veri akışı DURDU (eklenti/Binance sekmesi?)."
                           + (f" Sebep: {br}." if br else "")
                           + " Bot bekliyor, emir göndermiyor.")
                time.sleep(3)
                continue
            if not data_ok:
                data_ok = True
                notify("🟢 Veri akışı geri geldi, bot çalışıyor.")

            with LOCK:
                if not TRACKER.initialized:
                    TRACKER.initialize(raw)
                    log(f"📌 Baseline: {len(raw)} mevcut pozisyon. Takip için arayüzden 'Takip Et'.")
                _RAW_TRADER = raw
                eff = [p for p in TRACKER.effective_positions(raw) if p["symbol"] not in SKIP_SYMBOLS]
                followed = set(TRACKER.followed)
                startup = set(TRACKER.startup_symbols)

            # Rate-limit dostu: döngü başına TEK futures_account çağrısı.
            # Fiyatlar zaten elimizde: trader verisi (mark_price) + hesap pozisyonları.
            my_balance, my_all = execu.account_snapshot()
            prices = {p["symbol"]: p.get("mark_price", 0.0) for p in raw}
            for s, v in my_all.items():
                prices.setdefault(s, v.get("mark_price", 0.0))
            # DELTA modeli: bot YALNIZCA kendi kopyaladığı payı (applied) yönetir;
            # senin manuel pozisyonuna dokunmaz. Emirler bot_pos'a göre üretilir.
            with LOCK:
                bot_pos = TRACKER.bot_positions()
            market_data = build_market_data(execu, {"positions": eff}, bot_pos, prices)

            manual = getattr(config, "MANUAL_BALANCE", 0.0) or 0.0
            # Ölçek paydası: PnL'siz wallet balance (tik-tik titremez -> whipsaw önler).
            wallet_balance = getattr(execu, "last_wallet_balance", my_balance)
            scale_balance = manual if manual > 0 else wallet_balance
            mult = getattr(config, "SIZE_MULTIPLIER", 1.0)
            # DELTA modeli: bot yalnızca trader size'ı değişince hareket eder; değişimi
            # GÜNCEL bakiyeye ölçekli uygular. Hedef bot boyutu = desired_copy(...).
            # plan_orders'a faktör olarak (hedef / trader_eff) veririz -> target = hedef.
            live_factor = compute_scale_factor(scale_balance, balance) * mult
            with LOCK:
                factor_by_symbol = {}
                for p in eff:
                    te = p["size"]
                    desired = TRACKER.desired_copy(p["symbol"], te, live_factor,
                                                   tol=config.REBALANCE_TOLERANCE)
                    if desired is not None and te > 1e-12:
                        factor_by_symbol[p["symbol"]] = desired / te
            # tolerans=0: whipsaw kapısı artık desired_copy içinde (REBALANCE_TOLERANCE ile);
            # plan_orders sadece hedefe ulaşmak için emir üretsin (çift kapı eksik kopya yapmasın).
            orders, scale = plan_orders(
                {"balance": balance, "positions": eff},
                scale_balance, bot_pos, market_data,
                tolerance=0.0, min_notional=config.MIN_NOTIONAL_USDT,
                size_multiplier=mult, factor_by_symbol=factor_by_symbol,
            )

            eff_map = {p["symbol"]: p["size"] for p in eff}
            tp = [{
                "symbol": p["symbol"], "side": p["side"], "size": p["size"],
                "leverage": p.get("leverage", 0),
                "followed": p["symbol"] in followed,
                "effective": round(eff_map.get(p["symbol"], 0.0), 6),
                "is_startup": p["symbol"] in startup,
                "skipped": p["symbol"] in SKIP_SYMBOLS,
                "isolated": p.get("isolated", False),
                "margin": round(p.get("margin", 0.0), 2),
                "entry_price": p.get("entry_price", 0.0),
                "mark_price": p.get("mark_price", 0.0),
                "liq_price": p.get("liq_price", 0.0),
                "margin_ratio": p.get("margin_ratio", 0.0),
                "pnl": round(p.get("pnl", 0.0), 2),
                "roi": p.get("roi", 0.0),
            } for p in raw]

            with LOCK:
                RUNTIME.update({
                    "status": "çalışıyor", "scale": round(scale, 6),
                    "trader_balance": round(balance, 2), "my_balance": round(my_balance, 2),
                    "trader_positions": tp,
                    "my_positions": [_enrich_mine(s, v) for s, v in my_all.items()],
                    "updated": datetime.now().strftime("%H:%M:%S"),
                })

            # Botun hedef kopya boyutu (applied güncellemesi için)
            open_side = {p["symbol"]: ("BUY" if p["side"] == "LONG" else "SELL") for p in eff}
            targets = {p["symbol"]: round_step(p["size"] * factor_by_symbol.get(p["symbol"], scale * mult),
                                               market_data.get(p["symbol"], {}).get("step_size", 0.0))
                       for p in eff}

            failed = set()
            success = set()
            for o in orders:
                sym = o["symbol"]
                is_increase = (o["side"] == open_side.get(sym))  # bot maruziyetini artıran emir
                tag = "[DRY]" if config.DRY_RUN else "[LIVE]"
                _px = prices.get(sym, 0.0)
                _lev = o.get("leverage") or 1
                _margin = (o["qty"] * _px / _lev) if (_px and _lev) else 0.0
                _detail = f"@ {_px:g} USDT · margin {_margin:.2f} USDT"
                log(f"{tag} {o['side']} {o['qty']} {sym} | {o['reason']} | {_detail}")
                if config.DRY_RUN:
                    continue
                if is_increase and sym in failed:
                    log(f"   -> ⏭️ {sym} kapatma başarısızdı, açma iptal edildi.")
                    continue
                try:
                    if is_increase:
                        execu.ensure_margin_type(sym)               # cross margin garanti
                        execu.ensure_leverage(sym, o.get("leverage"))
                    # reduceOnly KULLANMA: manuel pozisyonla netleşmede reddi önler (delta modeli)
                    res = execu.market_order(sym, o["side"], o["qty"], False)
                    log(f"   -> OK orderId={res.get('orderId')}")
                    notify(f"✅ EMİR: {o['side']} {o['qty']} {sym} | {o['reason']} | {_detail}")
                    success.add(sym)
                except Exception as e:
                    m = str(e)
                    log(f"   -> ❌ {m}")
                    notify(f"❌ Emir hatası {sym}: {m[:120]}", key=f"err-{sym}", min_gap=120)
                    failed.add(sym)
                    if "-4411" in m:
                        SKIP_SYMBOLS.add(sym)
                        log(f"   -> ⏭️ {sym} atlanacak (TradFi-Perps).")

            # Başarılı emir gönderilen sembollerde botun kopya payını (applied) güncelle
            # ve delta senkron referansını (tref) ilerlet (başarısızsa sonraki döngüde tekrar).
            if success:
                with LOCK:
                    for sym in success:
                        TRACKER.set_applied(sym, targets.get(sym, 0.0))
                        if sym in eff_map:
                            TRACKER.commit_sync(sym, eff_map[sym])

            # YETİM KAPATMA: trader artık tutmadığı halde botun applied'i olan semboller.
            # (Trader tamamen kapatınca instance open=False olur; bot_pos/section D göremez.)
            # Gerçek pozisyon varsa reduceOnly market ile kapat; yoksa sadece defteri temizle.
            trader_syms_all = {p["symbol"] for p in raw}
            with LOCK:
                applied_list = TRACKER.applied_instances()
            for sym, side, app in applied_list:
                if sym in trader_syms_all:
                    continue  # trader hâlâ tutuyor -> normal akış yönetir
                real = my_all.get(sym)
                if real and real.get("size", 0) > 0 and not config.DRY_RUN:
                    cs = "SELL" if real["side"] == "LONG" else "BUY"
                    qty = round_step(min(app, real["size"]), execu.step_size(sym))
                    if qty > 0:
                        try:
                            execu.market_order(sym, cs, qty, True)  # reduceOnly: sadece kapat
                            log(f"[LIVE] {cs} {qty} {sym} | trader kapattı -> KAPAT (yetim)")
                            notify(f"✅ KAPAT (yetim): {cs} {qty} {sym} | trader kapattı")
                        except Exception as e:
                            log(f"   -> ❌ yetim kapatma {sym}: {str(e)[:100]}")
                            continue  # applied'i koru, sonraki döngüde tekrar dene
                with LOCK:
                    TRACKER.clear_applied(sym)  # kapatıldı ya da zaten flat -> defteri temizle

            # Büyük zarar uyarısı: yönetilen pozisyonların toplam PnL'i eşiği geçerse.
            alert_loss = getattr(config, "ALERT_LOSS_USDT", 0.0) or 0.0
            if alert_loss > 0:
                managed_syms = set(bot_pos) | set(targets)
                tot_pnl = sum(v.get("pnl", 0.0) for s, v in my_all.items() if s in managed_syms)
                if tot_pnl <= -alert_loss:
                    notify(f"🔻 BÜYÜK ZARAR: yönetilen pozisyon PnL {tot_pnl:.2f} USDT "
                           f"(eşik -{alert_loss:.0f}). 🛑 Hepsini Kapat'ı düşün.",
                           key="loss", min_gap=600)

        except Exception as e:
            m = str(e)
            if "-1003" in m:
                # Paylaşımlı IP rate limit -> geri çekil, baskı yapma.
                log("⏳ Rate limit. 30 sn geri çekiliyor.")
                with LOCK: RUNTIME["status"] = "rate limit (geri çekildi)"
                notify("⏳ Rate limit (-1003). Bot 30 sn geri çekildi.", key="rl", min_gap=300)
                time.sleep(30)
                continue
            log(f"🚨 {m}")
            notify(f"🚨 Bot hatası: {m[:150]}", key="err", min_gap=120)
        time.sleep(config.POLL_INTERVAL_SEC)


# ---------------------------------------------------------------- web sunucu
app = Flask(__name__)


TOKEN = getattr(config, "API_TOKEN", "")


def _auth_ok():
    return TOKEN and request.headers.get("X-TBB-Token") == TOKEN


@app.route("/ingest", methods=["POST"])
def ingest():
    # Token doğrulaması: kötü niyetli web sayfalarının sahte veri göndermesini engeller.
    if not _auth_ok():
        return ("forbidden", 403)
    d = request.get_json(silent=True)
    if not isinstance(d, dict) or "positions" not in d:
        return ("bad request", 400)  # boş/yanlış gövde ile pozisyonları sıfırlama
    with LOCK:
        INGESTED.update(ts=time.time(),
                        balance=float(d.get("balance", 0) or 0),
                        positions=_normalize(d.get("positions", [])))
    return jsonify(ok=True)


@app.route("/bridge", methods=["POST"])
def bridge():
    # Eklenti köprü durumunu (neden veri gönderemediğini) buraya bildirir. Emir mantığını
    # etkilemez; sadece teşhis/görünürlük içindir.
    if not _auth_ok():
        return ("forbidden", 403)
    d = request.get_json(silent=True) or {}
    reason = str(d.get("reason", ""))[:200]
    with LOCK:
        BRIDGE.update(ts=time.time(), reason=reason)
    return jsonify(ok=True)


@app.route("/api/state")
def api_state():
    with LOCK:
        return jsonify(dict(RUNTIME))


def _patch_followed(sym, val):
    """UI anında yansısın diye RUNTIME snapshot'taki followed bayrağını da güncelle."""
    for p in RUNTIME["trader_positions"]:
        if p["symbol"] == sym:
            p["followed"] = val


@app.route("/api/follow", methods=["POST"])
def api_follow():
    if not _auth_ok():
        return ("forbidden", 403)
    sym = (request.get_json(silent=True) or {}).get("symbol", "")
    with LOCK:
        TRACKER.follow(sym, _RAW_TRADER)
        _patch_followed(sym, True)
    log(f"👁️ Takibe alındı: {sym} (bu andan sonraki değişiklikler kopyalanacak)")
    return jsonify(ok=True)


@app.route("/api/copy", methods=["POST"])
def api_copy():
    if not _auth_ok():
        return ("forbidden", 403)
    sym = (request.get_json(silent=True) or {}).get("symbol", "")
    with LOCK:
        TRACKER.copy(sym, _RAW_TRADER)
        _patch_followed(sym, True)
    log(f"📥 Kopyalandı: {sym} (mevcut pozisyon ölçekli açılıp takibe alındı)")
    return jsonify(ok=True)


@app.route("/api/unfollow", methods=["POST"])
def api_unfollow():
    if not _auth_ok():
        return ("forbidden", 403)
    sym = (request.get_json(silent=True) or {}).get("symbol", "")
    with LOCK:
        TRACKER.unfollow(sym)
        _patch_followed(sym, False)
    log(f"🚫 Takip bırakıldı: {sym}")
    return jsonify(ok=True)


@app.route("/api/panic", methods=["POST"])
def api_panic():
    """ACİL: bot'un yönettiği TÜM pozisyonları market ile kapat + takibi bırak."""
    if not _auth_ok():
        return ("forbidden", 403)
    if EXECU is None:
        return jsonify(ok=False, error="bot hazır değil")
    with LOCK:
        managed = TRACKER.managed_symbols()
    closed = []
    try:
        _bal, pos = EXECU.account_snapshot()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:120])
    for s, v in pos.items():
        if s in managed and v["size"] > 0:
            side = "SELL" if v["side"] == "LONG" else "BUY"
            try:
                EXECU.market_order(s, side, v["size"], True)
                closed.append(s)
            except Exception as e:
                log(f"🛑 Panik kapatma hatası {s}: {str(e)[:80]}")
    with LOCK:
        for s in list(TRACKER.followed):
            TRACKER.unfollow(s)
    log(f"🛑 ACİL KAPATMA: {len(closed)} pozisyon kapatıldı, tüm takip bırakıldı.")
    notify(f"🛑 ACİL KAPATMA: {len(closed)} pozisyon kapatıldı ({', '.join(closed) or '-'}), takip bırakıldı.")
    return jsonify(ok=True, closed=closed)


@app.route("/")
def index():
    return PAGE.replace("__TOKEN__", TOKEN)


@app.route("/logo.png")
def logo():
    """Arayüz logosu: proje kökündeki turtle.png. Yoksa 404 -> arayüz 🐢 emojisine düşer."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "turtle.png")
    return send_file(p) if os.path.exists(p) else ("", 404)


PAGE = """<!doctype html><html lang=tr><head><meta charset=utf-8>
<title>Turtle Stance</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{
 --bg:#0a0c10; --surface:#11151b; --surface2:#161b22; --line:#222a35; --line2:#2c3744;
 --text:#e6edf3; --muted:#8b97a6; --faint:#5c6775;
 --green:#22c55e; --red:#f6465d; --blue:#3b82f6;
 --green-dim:rgba(34,197,94,.40); --blue-dim:rgba(59,130,246,.40);
 --radius:14px; color-scheme:dark;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:ui-sans-serif,-apple-system,"Segoe UI",Inter,system-ui,sans-serif;
 background:radial-gradient(1200px 600px at 80% -10%,#13202e 0%,transparent 60%),var(--bg);
 color:var(--text);font-size:14px;line-height:1.45;-webkit-font-smoothing:antialiased;padding:14px 16px 40px}
.mono{font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
.wrap{max-width:1500px;margin:0 auto}
/* header */
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:9px}
.logo{width:30px;height:30px;border-radius:8px;background:linear-gradient(135deg,#3b82f6,#22c55e);
 display:flex;align-items:center;justify-content:center;font-size:14px;object-fit:cover}
.brand h1{font-size:14px;font-weight:650;letter-spacing:-.01em}
.brand p{font-size:11px;color:var(--muted)}
.live{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--muted);
 background:var(--surface);border:1px solid var(--line);padding:5px 11px;border-radius:999px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--faint)}
.dot.ok{background:var(--green);box-shadow:0 0 0 0 rgba(34,197,94,.5);animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(34,197,94,.45)}70%{box-shadow:0 0 0 7px rgba(34,197,94,0)}100%{box-shadow:0 0 0 0 rgba(34,197,94,0)}}
/* stat strip */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:1px;
 background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-bottom:16px}
.stat{background:var(--surface);padding:8px 12px}
.stat .k{font-size:10px;color:var(--muted);letter-spacing:.03em;text-transform:uppercase;margin-bottom:2px}
.stat .v{font-size:14px;font-weight:600;letter-spacing:-.01em}
/* section */
.sec{display:flex;align-items:center;gap:9px;margin:0 0 12px}
.sec h2{font-size:13px;font-weight:600;letter-spacing:.03em;text-transform:uppercase;color:var(--muted)}
.sec .ln{height:1px;flex:1;background:linear-gradient(90deg,var(--line),transparent)}
.bardot{width:7px;height:7px;border-radius:2px}
.bardot.b{background:var(--blue)} .bardot.g{background:var(--green)}
/* cards */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,290px),1fr));gap:10px;margin-bottom:22px}
.card{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
 padding:11px 13px;overflow:hidden;transition:transform .15s ease,border-color .15s ease}
.card:hover{transform:translateY(-2px);border-color:var(--line2)}
.card::before{content:"";position:absolute;inset:0 0 auto 0;height:2px}
.card.trader::before{background:linear-gradient(90deg,var(--blue),transparent)}
.card.mine::before{background:linear-gradient(90deg,var(--green),transparent)}
.card.trader{border-color:var(--blue-dim)} .card.mine{border-color:var(--green-dim)}
.card.skip{opacity:.45}
.chead{display:flex;align-items:center;gap:7px;margin-bottom:11px;flex-wrap:wrap}
.sb{width:18px;height:18px;border-radius:5px;display:flex;align-items:center;justify-content:center;
 font-size:10px;font-weight:700;color:#fff}
.sb.s{background:var(--red)} .sb.l{background:var(--green)}
.sym{font-size:14px;font-weight:650;letter-spacing:-.01em}
.badge{font-size:10px;color:var(--muted);border:1px solid var(--line2);border-radius:5px;padding:1px 6px}
.spacer{flex:1}
.row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:9px}
.row.two{grid-template-columns:1fr 1fr}
.f .k{font-size:10px;color:var(--muted);margin-bottom:2px;white-space:nowrap}
.f .v{font-size:13px;font-weight:550;white-space:nowrap}
.f.r{text-align:right}
.pos{color:var(--green)} .neg{color:var(--red)}
.foot{display:flex;align-items:center;justify-content:space-between;gap:8px;
 border-top:1px solid var(--line);padding-top:9px;margin-top:1px}
.pill{font-size:11px;padding:3px 9px;border-radius:999px;background:var(--surface2);color:var(--muted);border:1px solid var(--line)}
.pill.on{background:rgba(59,130,246,.12);color:#7eb0ff;border-color:var(--blue-dim)}
.pill.new{background:rgba(34,197,94,.12);color:#5fe39a;border-color:var(--green-dim)}
.copyable{font-size:11px;color:var(--faint)}
button{cursor:pointer;border:none;border-radius:8px;padding:7px 16px;font-size:13px;font-weight:600;
 transition:filter .15s ease;font-family:inherit}
button:hover{filter:brightness(1.1)}
.b-follow{background:var(--surface2);color:var(--text);border:1px solid var(--line2)}
.b-copy{background:var(--green);color:#04210f}
.b-unfollow{background:var(--surface2);color:var(--text);border:1px solid var(--line2)}
.btns{display:flex;gap:6px}
.empty{color:var(--faint);font-size:13px;padding:24px;text-align:center;border:1px dashed var(--line);border-radius:var(--radius);grid-column:1/-1}
/* log */
.log{background:#06090d;border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px;height:260px;
 overflow:auto;font:12px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;color:#93a1b0}
</style></head><body><div class=wrap>

<div class=top>
 <div class=brand>
   <img class=logo src="/logo.png" alt="" onerror="this.outerHTML='<div class=logo>🐢</div>'">
   <div><h1>Turtle Stance</h1><p id=head>yükleniyor…</p></div>
 </div>
 <div style="display:flex;gap:10px;align-items:center">
   <div class=live><span class=dot id=livedot></span><span id=livetxt>—</span></div>
   <button onclick="panic()" style="background:#f6465d;color:#fff">🛑 Hepsini Kapat</button>
 </div>
</div>

<div class=stats id=stats></div>

<div class=sec><span class="bardot b"></span><h2>Trader pozisyonları</h2><span class=ln></span></div>
<div id=traderCards class=grid></div>

<div class=sec><span class="bardot g"></span><h2>Bizim pozisyonlarımız</h2><span class=ln></span></div>
<div id=myCards class=grid></div>

<div class=sec><h2>Canlı log</h2><span class=ln></span></div>
<div id=log class=log></div>

</div>
<script>
const num=(x,d=2)=>Number(x||0).toLocaleString('en-US',{minimumFractionDigits:d>2?0:d,maximumFractionDigits:d});
const sgn=x=>(x>=0?'+':'')+num(x);
const cl=x=>x>=0?'pos':'neg';
const TOKEN="__TOKEN__";
async function post(u,s){await fetch(u,{method:'POST',headers:{'Content-Type':'application/json','X-TBB-Token':TOKEN},body:JSON.stringify({symbol:s})});refresh()}
async function panic(){
 if(!confirm('Bot\\'un yönettiği TÜM pozisyonlar market ile KAPATILACAK ve takip bırakılacak. Emin misin?'))return;
 let r=await (await fetch('/api/panic',{method:'POST',headers:{'Content-Type':'application/json','X-TBB-Token':TOKEN},body:'{}'})).json();
 alert(r.ok?('Kapatıldı: '+(r.closed||[]).join(', ')||'yönetilen pozisyon yoktu'):('Hata: '+(r.error||'?')));refresh()}
function chead(p){let l=p.side==='SHORT'?'s':'l',extra=arguments[1]||'';
 return `<div class=chead><span class="sb ${l}">${l.toUpperCase()}</span><span class=sym>${p.symbol}</span>
  <span class=badge>Perp</span><span class=badge>${p.isolated?'Isolated':'Cross'}</span><span class=badge>${p.leverage}x</span><span class=spacer></span>${extra}</div>`}
function prices(p){return `<div class=row>
  <div class=f><div class=k>Entry Price (USDT)</div><div class="v mono">${num(p.entry_price,4)}</div></div>
  <div class=f><div class=k>Mark Price (USDT)</div><div class="v mono">${num(p.mark_price,4)}</div></div>
  <div class="f r"><div class=k>Liq.Price (USDT)</div><div class="v mono">${num(p.liq_price,4)}</div></div></div>`}
function traderCard(p){
 let coin=p.symbol.replace('USDT',''),
  durum=p.skipped?'<span class=pill>atlandı</span>'
   :(!p.is_startup?'<span class="pill new">yeni</span> ':'')+(p.followed?'<span class="pill on">takip</span>':'<span class=pill>pasif</span>'),
  btn=p.skipped?'':(p.followed
   ?`<button class=b-unfollow onclick="post('/api/unfollow','${p.symbol}')">Bırak</button>`
   :`<div class=btns><button class=b-follow onclick="post('/api/follow','${p.symbol}')">Takip Et</button>`
    +`<button class=b-copy onclick="post('/api/copy','${p.symbol}')">Kopyala</button></div>`);
 return `<div class="card trader ${p.skipped?'skip':''}">${chead(p)}
  <div class="row two">
   <div class=f><div class=k>PnL (USDT)</div><div class="v mono ${cl(p.pnl)}">${sgn(p.pnl)}</div></div>
   <div class="f r"><div class=k>ROI</div><div class="v mono ${cl(p.roi)}">${sgn(p.roi*100)}%</div></div></div>
  <div class=row>
   <div class=f><div class=k>Size</div><div class="v mono">${num(p.size,4)} ${coin}</div><div class=k style="margin-top:3px">${num(p.size*p.mark_price)} USDT</div></div>
   <div class=f><div class=k>Margin (USDT)</div><div class="v mono">${num(p.margin)}</div></div>
   <div class="f r"><div class=k>Margin Ratio</div><div class="v mono">${num(p.margin_ratio)}%</div></div></div>
  ${prices(p)}
  <div class=foot><span>${durum} <span class=copyable>· kopyalanabilir ${p.effective}</span></span>${btn}</div></div>`}
function mineCard(p){let coin=p.symbol.replace('USDT',''),
 mr=p.margin_ratio==null?'—':num(p.margin_ratio)+'%';
 return `<div class="card mine">${chead(p)}
  <div class="row two">
   <div class=f><div class=k>PnL (USDT)</div><div class="v mono ${cl(p.pnl)}">${sgn(p.pnl)}</div></div>
   <div class="f r"><div class=k>ROI</div><div class="v mono ${cl(p.roi)}">${sgn(p.roi*100)}%</div></div></div>
  <div class=row>
   <div class=f><div class=k>Size</div><div class="v mono">${num(p.size,4)} ${coin}</div><div class=k style="margin-top:3px">${num(p.size*p.mark_price)} USDT</div></div>
   <div class=f><div class=k>Margin (USDT)</div><div class="v mono">${num(p.margin)}</div></div>
   <div class="f r"><div class=k>Margin Ratio</div><div class="v mono">${mr}</div></div></div>
  ${prices(p)}</div>`}
async function refresh(){
 let d=await (await fetch('/api/state')).json();
 let live=d.status==='çalışıyor';
 document.getElementById('livedot').className='dot'+(live?' ok':'');
 document.getElementById('livetxt').textContent=d.status;
 document.getElementById('head').textContent='son güncelleme '+(d.updated||'—');
 let mode=d.dry_run?'TEST':'CANLI';
 document.getElementById('stats').innerHTML=`
  <div class=stat><div class=k>Ölçek faktörü</div><div class="v mono">${d.scale}</div></div>
  <div class=stat><div class=k>Trader bakiye (USDT)</div><div class="v mono">${num(d.trader_balance)}</div></div>
  <div class=stat><div class=k>Bizim bakiye (USDT)</div><div class="v mono">${num(d.my_balance)}</div></div>
  <div class=stat><div class=k>Mod</div><div class=v>${mode}</div></div>
  <div class=stat><div class=k>Takip / pozisyon</div><div class="v mono">${d.trader_positions.filter(p=>p.followed).length} / ${d.my_positions.length}</div></div>`;
 document.getElementById('traderCards').innerHTML=d.trader_positions.map(traderCard).join('')||'<div class=empty>Veri bekleniyor — eklenti ve Binance sekmesi açık mı?</div>';
 // Bizim kartları trader sırasıyla aynı sütuna hizala: önce trader sembol sırası, sonra fazlalıklar.
 let order=d.trader_positions.map(p=>p.symbol);
 let mine=d.my_positions.slice().sort((a,b)=>{
   let ia=order.indexOf(a.symbol), ib=order.indexOf(b.symbol);
   return (ia<0?1e9:ia)-(ib<0?1e9:ib);
 });
 document.getElementById('myCards').innerHTML=mine.map(mineCard).join('')||'<div class=empty>Açık pozisyon yok</div>';
 let lg=document.getElementById('log');lg.textContent=(d.log||[]).join('\\n');lg.scrollTop=lg.scrollHeight;
}
refresh();setInterval(refresh,2500);
</script></body></html>"""


if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    print("\n🌐 Arayüz: http://127.0.0.1:8777\n")
    app.run(host="127.0.0.1", port=8777, debug=False, use_reloader=False, threaded=True)
