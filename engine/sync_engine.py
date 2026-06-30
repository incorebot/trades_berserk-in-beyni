"""
Senkronizasyon motoru — SAF MANTIK. Binance'e veya ağa bağlı değildir; bu yüzden
mock veriyle tam test edilebilir.

Girdi: trader snapshot + bizim bakiye/pozisyonlar + sembol piyasa verisi.
Çıktı: gönderilecek emir niyetleri listesi (henüz GÖNDERİLMEZ).
"""

import math


def compute_scale_factor(my_balance, trader_balance):
    """Risk ölçek oranı = bizim bakiye / trader bakiye."""
    if trader_balance <= 0:
        return 0.0
    return my_balance / trader_balance


def round_step(quantity, step_size):
    """Miktarı borsa lot adımına göre AŞAĞI yuvarlar (fazla emir göndermemek için)."""
    if step_size <= 0:
        return quantity
    precision = max(0, int(round(-math.log10(step_size))))
    return round(math.floor(quantity / step_size) * step_size, precision)


def _notional_ok(qty, price, min_notional):
    return price <= 0 or (qty * price) >= min_notional


def plan_orders(trader_state, my_balance, my_positions, market_data,
                tolerance=0.10, min_notional=5.0, size_multiplier=1.0,
                factor_by_symbol=None):
    """Hedef ile mevcut durumu karşılaştırıp emir niyetleri üretir.

    Parametreler:
      trader_state : {'balance': float, 'positions': [{symbol, side, size, leverage}]}
      my_balance   : float — bizim toplam margin balance
      my_positions : {symbol: {'side': 'LONG'/'SHORT', 'size': float}}
      market_data  : {symbol: {'step_size': float, 'price': float, 'leverage': int}}
      tolerance    : hedeften bu orandan az sapma için emir gönderme (whipsaw önler)
      min_notional : bu USDT değerinin altındaki emirleri atla

    Döndürür: (orders, scale_factor)
      orders: [{symbol, side('BUY'/'SELL'), qty, reduce_only(bool), reason, leverage}]
    """
    scale = compute_scale_factor(my_balance, trader_state["balance"])
    orders = []
    if scale <= 0:
        return orders, scale

    trader_syms = set()

    for pos in trader_state["positions"]:
        sym = pos["symbol"]
        trader_syms.add(sym)
        md = market_data.get(sym, {})
        step = md.get("step_size", 0.0)
        price = md.get("price", 0.0)

        # Kilitli faktör verilmişse onu kullan (açık pozisyon bakiye değişiminden ETKİLENMEZ);
        # yoksa canlı ölçek×çarpan (geriye dönük uyumluluk / testler).
        fac = (factor_by_symbol or {}).get(sym)
        if fac is None:
            fac = scale * size_multiplier
        target = round_step(pos["size"] * fac, step)

        cur = my_positions.get(sym)
        cur_side = cur["side"] if cur else "NONE"
        cur_size = cur["size"] if cur else 0.0

        # --- A) Reversal: yön değiştiyse önce mevcut pozisyonu tamamen kapat ---
        if cur_side != "NONE" and cur_side != pos["side"]:
            orders.append(_order(sym, _close_side(cur_side), cur_size, True,
                                 f"reversal: {cur_side} kapat", pos["leverage"]))
            cur_side, cur_size = "NONE", 0.0

        # --- B) Yeni giriş ---
        if cur_side == "NONE":
            if target > 0 and _notional_ok(target, price, min_notional):
                orders.append(_order(sym, _open_side(pos["side"]), target, False,
                                     f"giriş {pos['side']}", pos["leverage"]))
            continue

        # --- C) Ölçek ayarı (DCA yukarı / kâr al aşağı) ---
        diff = target - cur_size
        if target <= 0 or abs(diff) / target < tolerance:
            continue  # tolerans içinde -> dokunma (PnL kaynaklı titremeyi yok say)

        qty = round_step(abs(diff), step)
        if qty <= 0 or not _notional_ok(qty, price, min_notional):
            continue

        if diff > 0:
            orders.append(_order(sym, _open_side(pos["side"]), qty, False,
                                 f"ölçek↑ +{qty}", pos["leverage"]))
        else:
            orders.append(_order(sym, _close_side(pos["side"]), qty, True,
                                 f"ölçek↓ -{qty}", pos["leverage"]))

    # --- D) Trader'ın kapattığı pozisyonlar: bizde varsa kapat ---
    for sym, cur in my_positions.items():
        if sym not in trader_syms and cur["size"] > 0:
            orders.append(_order(sym, _close_side(cur["side"]), cur["size"], True,
                                 "trader kapattı -> kapat", None))

    return orders, scale


def _open_side(side):
    return "BUY" if side == "LONG" else "SELL"


def _close_side(side):
    return "SELL" if side == "LONG" else "BUY"


def _order(symbol, side, qty, reduce_only, reason, leverage):
    return {
        "symbol": symbol, "side": side, "qty": qty,
        "reduce_only": reduce_only, "reason": reason, "leverage": leverage,
    }
