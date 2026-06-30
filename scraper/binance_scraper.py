"""
Binance smart-money profilinden trader'ın açık pozisyonlarını ve toplam bakiyesini
çeker. Bu katman SALT-OKUNUR'dur; hiçbir emir göndermez.

İki private web endpoint'i kullanır (giriş yapmış oturum cookie'si gerektirir):
  - query-positions  : açık pozisyonlar (UM = USDⓈ-M, CM = Coin-M)
  - chart-data BALANCE: günlük bakiye serisi -> son nokta = güncel toplam bakiye
"""

import json
import urllib.request
import urllib.error

_BASE = "https://www.binance.com/bapi/asset/v1"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


class ScraperError(Exception):
    """Scraper veri çekemediğinde fırlatılır. Bot bunu yakalayıp emir göndermemeli."""


def _headers(cookie, csrf):
    return {
        "accept": "*/*",
        "clienttype": "web",
        "content-type": "application/json",
        "lang": "en-TR",
        "csrftoken": csrf,
        "cookie": cookie,
        "user-agent": _UA,
        "referer": "https://www.binance.com/en-TR/smart-money/profile/",
    }


def _get(url, cookie, csrf, timeout=20):
    req = urllib.request.Request(url, headers=_headers(cookie, csrf))
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        if e.code == 401:
            raise ScraperError(
                "401: Oturum geçersiz/expire. config.py'deki cookie ve csrf token yenilenmeli."
            )
        raise ScraperError(f"HTTP {e.code}: {body}")
    except Exception as e:
        raise ScraperError(f"Ağ hatası: {e}")

    try:
        data = json.loads(raw)
    except ValueError:
        raise ScraperError(f"JSON parse hatası: {raw[:200]}")

    if not data.get("success"):
        raise ScraperError(f"API başarısız: code={data.get('code')} msg={data.get('message')}")
    return data


def fetch_positions(trader_id, cookie, csrf, market_type="UM"):
    """Belirtilen market'teki açık pozisyonları normalize edilmiş listede döndürür.

    Her öğe: {symbol, side ('LONG'/'SHORT'), size (>0), leverage, margin, market_type}
    """
    url = (f"{_BASE}/private/future/smart-money/profile/query-positions"
           f"?topTraderId={trader_id}&marketType={market_type}&page=1&rows=50")
    data = _get(url, cookie, csrf)

    positions = []
    for item in data.get("data") or []:
        amount = float(item["amount"])
        if amount == 0:
            continue
        positions.append({
            "symbol": item["symbol"],
            "side": "LONG" if amount > 0 else "SHORT",
            "size": abs(amount),
            "leverage": int(item.get("leverage") or 0),
            "margin": float(item.get("margin") or 0.0),
            "entry_price": float(item.get("entryPrice") or 0.0),
            "market_type": market_type,
        })
    return positions


def fetch_trader_balance(trader_id, cookie, csrf):
    """Trader'ın en güncel toplam bakiyesini (USDT) döndürür.

    chart-data günlük noktalar verir; son nokta en güncel değerdir. Bu değer
    intraday PnL ile tam senkron değildir ama scale factor için yeterli yaklaşıklıktır.
    """
    url = (f"{_BASE}/friendly/future/smart-money/profile/chart-data"
           f"?topTraderId={trader_id}&timeRange=30D&chartDataType=BALANCE")
    data = _get(url, cookie, csrf)
    items = (data.get("data") or {}).get("items") or []
    if not items:
        raise ScraperError("Bakiye serisi boş döndü.")
    return float(items[-1][1])


def get_trader_state(trader_id, cookie, csrf, include_coin_m=False):
    """Botun tükettiği tek snapshot: {balance, positions[]}."""
    positions = fetch_positions(trader_id, cookie, csrf, "UM")
    if include_coin_m:
        positions += fetch_positions(trader_id, cookie, csrf, "CM")
    return {
        "balance": fetch_trader_balance(trader_id, cookie, csrf),
        "positions": positions,
    }


if __name__ == "__main__":
    # Hızlı manuel test: config.py'den okur, sadece ekrana basar.
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import config
    state = get_trader_state(
        config.TOP_TRADER_ID, config.BINANCE_WEB_COOKIE, config.BINANCE_CSRF_TOKEN,
        include_coin_m=config.COPY_COIN_M,
    )
    print(f"Trader bakiye: {state['balance']:.2f} USDT")
    for p in state["positions"]:
        print(f"  {p['symbol']:14} {p['side']:5} size={p['size']} lev={p['leverage']}x margin={p['margin']:.1f}")
