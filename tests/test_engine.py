"""Motorun saf mantığını mock veriyle test eder. Borsa/ağ gerektirmez."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.sync_engine import plan_orders, compute_scale_factor, round_step

# Bizim bakiye trader'ın 1/100'ü -> scale = 0.01
MY_BAL = 1784.16
TRADER = {"balance": 178416.0, "positions": [
    {"symbol": "TAOUSDT", "side": "SHORT", "size": 190.839, "leverage": 10},
    {"symbol": "XAUUSDT", "side": "LONG",  "size": 71.064,  "leverage": 25},
]}
MD = {
    "TAOUSDT": {"step_size": 0.001, "price": 266.0, "leverage": 10},
    "XAUUSDT": {"step_size": 0.001, "price": 4350.0, "leverage": 25},
}

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1; print(f"  ✅ {name}")
    else: failed += 1; print(f"  ❌ {name}")

print("scale & round:")
check("scale=0.01", abs(compute_scale_factor(MY_BAL, 178416.0) - 0.01) < 1e-6)
check("round aşağı", round_step(1.9089, 0.001) == 1.908)
check("trader_balance=0 -> 0", compute_scale_factor(100, 0) == 0.0)

print("\nCASE A - sıfırdan giriş:")
o, s = plan_orders(TRADER, MY_BAL, {}, MD)
syms = {x["symbol"]: x for x in o}
check("2 emir", len(o) == 2)
check("TAO SELL (short giriş)", syms["TAOUSDT"]["side"] == "SELL")
check("XAU BUY (long giriş)", syms["XAUUSDT"]["side"] == "BUY")
check("TAO qty ~1.908", abs(syms["TAOUSDT"]["qty"] - 1.908) < 1e-6)

print("\nCASE B - tolerans içinde (dokunma):")
mine = {"TAOUSDT": {"side": "SHORT", "size": 1.908}, "XAUUSDT": {"side": "LONG", "size": 0.710}}
o, s = plan_orders(TRADER, MY_BAL, mine, MD)
check("emir yok", len(o) == 0)

print("\nCASE C - trader ekledi (DCA yukarı):")
mine = {"TAOUSDT": {"side": "SHORT", "size": 1.0}, "XAUUSDT": {"side": "LONG", "size": 0.710}}
o, s = plan_orders(TRADER, MY_BAL, mine, MD)
tao = [x for x in o if x["symbol"] == "TAOUSDT"]
check("TAO ölçek↑ emri", len(tao) == 1 and tao[0]["side"] == "SELL" and not tao[0]["reduce_only"])

print("\nCASE D - reversal (short->long):")
mine = {"TAOUSDT": {"side": "LONG", "size": 5.0}}
o, s = plan_orders(TRADER, MY_BAL, mine, MD)
tao = [x for x in o if x["symbol"] == "TAOUSDT"]
# LONG kapatmak = SELL(reduce), SHORT açmak = SELL -> ikisi de SELL
check("önce kapat (SELL reduce) sonra aç (SELL)",
      len(tao) == 2 and tao[0]["side"] == "SELL" and tao[0]["reduce_only"]
      and tao[1]["side"] == "SELL" and not tao[1]["reduce_only"])

print("\nCASE E - trader kapattı:")
mine = {"DOGEUSDT": {"side": "LONG", "size": 100.0}}
o, s = plan_orders(TRADER, MY_BAL, mine, {**MD, "DOGEUSDT": {"step_size": 1.0, "price": 0.2}})
doge = [x for x in o if x["symbol"] == "DOGEUSDT"]
check("DOGE kapat (SELL reduce)", len(doge) == 1 and doge[0]["side"] == "SELL" and doge[0]["reduce_only"])

print("\nCASE F - min_notional altı atlanır:")
o, s = plan_orders(TRADER, 1.0, {}, MD)  # bakiye çok küçük -> minik emirler
check("notional altı emirler atlandı", all(x["qty"] * MD[x["symbol"]]["price"] >= 5.0 for x in o))

print(f"\n{'='*40}\nSONUÇ: {passed} geçti, {failed} kaldı")
sys.exit(1 if failed else 0)
