"""CopyTracker testleri: baseline + kullanıcı kontrollü takip + yönetilen semboller."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.tracker import CopyTracker

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1; print(f"  ✅ {name}")
    else: failed += 1; print(f"  ❌ {name}")

def pos(sym, side, size): return {"symbol": sym, "side": side, "size": size, "leverage": 10}

print("baseline init -> mevcutlar takip EDİLMEZ:")
t = CopyTracker()
start = [pos("TAOUSDT","SHORT",287.0), pos("XAUUSDT","LONG",71.0)]
t.initialize(start)
eff = t.effective_positions(start)
check("ilk durumda effective boş", eff == [])
eff = t.effective_positions([pos("TAOUSDT","SHORT",350.0)])  # takip yokken artış
check("takip edilmeyen sembol artışı kopyalanmaz", eff == [])

print("\ntakibe alınca mevcut AÇILMAZ, sonraki değişim kopyalanır:")
t.follow("TAOUSDT", [pos("TAOUSDT","SHORT",350.0)])  # baseline 350
eff = t.effective_positions([pos("TAOUSDT","SHORT",350.0)])
check("takip anında effective 0 (açma yok)", all(p["symbol"]!="TAOUSDT" for p in eff))
eff = t.effective_positions([pos("TAOUSDT","SHORT",400.0)])  # +50
tao = [p for p in eff if p["symbol"]=="TAOUSDT"]
check("büyütünce +50 kopyalanır", len(tao)==1 and abs(tao[0]["size"]-50.0)<1e-9)
eff = t.effective_positions([pos("TAOUSDT","SHORT",370.0)])  # 400->370 -> 20
tao = [p for p in eff if p["symbol"]=="TAOUSDT"]
check("azaltınca effective 20 (370-350)", len(tao)==1 and abs(tao[0]["size"]-20.0)<1e-9)
eff = t.effective_positions([pos("TAOUSDT","SHORT",350.0)])
check("baseline'a dönünce effective 0", all(p["symbol"]!="TAOUSDT" for p in eff))

print("\nYENİ sembol (bot çalışırken açılan) -> otomatik tam kopya:")
eff = t.effective_positions([pos("DOGEUSDT","LONG",1000.0)])
doge = [p for p in eff if p["symbol"]=="DOGEUSDT"]
check("DOGE otomatik takip, effective 1000", len(doge)==1 and doge[0]["size"]==1000.0)
check("DOGE followed kümesinde", "DOGEUSDT" in t.followed)

print("\nunfollow -> takip biter:")
t.unfollow("DOGEUSDT")
check("DOGE followed'dan çıktı", "DOGEUSDT" not in t.followed)

print("\nyön değişimi (takip edilen sembolde) -> tam kopya:")
t2 = CopyTracker(); t2.initialize([pos("TAOUSDT","SHORT",287.0)])
t2.follow("TAOUSDT", [pos("TAOUSDT","SHORT",287.0)])
eff = t2.effective_positions([pos("TAOUSDT","LONG",10.0)])
tao = [p for p in eff if p["symbol"]=="TAOUSDT"]
check("yön değişince effective = 10", len(tao)==1 and tao[0]["size"]==10.0)

print("\nyalnızca yönetilen semboller -> diğer hesap pozisyonlarına dokunma:")
t3 = CopyTracker(); t3.initialize([])
eff = t3.effective_positions([pos("TAOUSDT","SHORT",350.0)])  # yeni -> auto
my_all = {"TAOUSDT":{"side":"SHORT","size":1.0}, "BTCUSDT":{"side":"LONG","size":0.5}}
my_managed = t3.select_managed_positions(eff, my_all)
check("yalnızca TAO yönetilir, BTC hariç", set(my_managed.keys())=={"TAOUSDT"})
t3.update_managed(eff, my_managed)
check("managed = {TAO}", t3.managed=={"TAOUSDT"})

print("\nkapat + yeniden aç -> farklı instance (eski takip taşınmaz):")
t4 = CopyTracker(); t4.initialize([pos("TAOUSDT","SHORT",100.0)])
iid1 = t4.open_by_symbol["TAOUSDT"]
t4.follow("TAOUSDT",[pos("TAOUSDT","SHORT",100.0)])
t4.effective_positions([])                 # trader kapattı -> instance kapanır
t4.effective_positions([pos("TAOUSDT","SHORT",50.0)])  # yeniden açtı -> yeni instance
iid2 = t4.open_by_symbol["TAOUSDT"]
check("yeniden açılış farklı instance id", iid1 != iid2)
check("yeni instance otomatik takip (çalışırken açıldı)", t4._open_inst("TAOUSDT")["followed"])

print("\nkalıcılık: state diske yazılır ve yeni tracker yükler:")
import tempfile
sf = tempfile.mktemp(suffix=".json")
ta = CopyTracker(state_file=sf); ta.initialize([pos("MSTRUSDT","SHORT",10.0)])
ta.follow("MSTRUSDT",[pos("MSTRUSDT","SHORT",10.0)])
tb = CopyTracker(state_file=sf)              # restart simülasyonu
tb.effective_positions([pos("MSTRUSDT","SHORT",10.0)])  # hâlâ açık -> eşleşir
check("restart sonrası takip korunur", "MSTRUSDT" in tb.followed)
os.remove(sf)

print("\nKOPYALA: baseline 0 -> mevcut hemen (effective=tam boyut):")
tc = CopyTracker(); tc.initialize([pos("BTCUSDT","SHORT",2.0)])
tc.copy("BTCUSDT", [pos("BTCUSDT","SHORT",2.0)])
eff = tc.effective_positions([pos("BTCUSDT","SHORT",2.0)])
btc = [p for p in eff if p["symbol"]=="BTCUSDT"]
check("copy sonrası effective = tam 2.0", len(btc)==1 and abs(btc[0]["size"]-2.0)<1e-9)

print("\nTAKİP ET: baseline current -> mevcut açılmaz (effective 0):")
tf = CopyTracker(); tf.initialize([pos("BTCUSDT","SHORT",2.0)])
tf.follow("BTCUSDT", [pos("BTCUSDT","SHORT",2.0)])
eff = tf.effective_positions([pos("BTCUSDT","SHORT",2.0)])
check("follow sonrası effective 0", all(p["symbol"]!="BTCUSDT" for p in eff))

print("\nbot_positions / set_applied (botun kendi payı):")
tf.set_applied("BTCUSDT", 0.05)
bp = tf.bot_positions()
check("bot_positions applied'i yansıtır", bp.get("BTCUSDT",{}).get("size")==0.05)
tf.set_applied("BTCUSDT", 0.0)
check("applied 0 -> bot_positions'tan düşer", "BTCUSDT" not in tf.bot_positions())

print("\nDELTA modeli (desired_copy): yalnızca trader size değişince, güncel bakiyeye ölçekli:")
td = CopyTracker(); td.initialize([])
# yeni pozisyon: trader 100, faktör 0.01 -> hedef 1.0 (güncel ölçekte aç)
eff = td.effective_positions([pos("ETHUSDT","LONG",100.0)])
te = [p for p in eff if p["symbol"]=="ETHUSDT"][0]["size"]
d = td.desired_copy("ETHUSDT", te, 0.01, tol=0.10)
check("yeni pozisyon güncel ölçekte açılır (1.0)", abs(d-1.0)<1e-9)
td.set_applied("ETHUSDT", d); td.commit_sync("ETHUSDT", te)  # emir başarılı simülasyonu
# BAKİYE DEĞİŞTİ (faktör 0.02) ama trader AYNI -> bot DOKUNMAZ
eff = td.effective_positions([pos("ETHUSDT","LONG",100.0)])
te = [p for p in eff if p["symbol"]=="ETHUSDT"][0]["size"]
d = td.desired_copy("ETHUSDT", te, 0.02, tol=0.10)
check("bakiye değişti trader aynı -> hedef değişmez (1.0)", abs(d-1.0)<1e-9)
# trader ARTIRDI 100->150, güncel faktör 0.02 -> 1.0 + 50*0.02 = 2.0
eff = td.effective_positions([pos("ETHUSDT","LONG",150.0)])
te = [p for p in eff if p["symbol"]=="ETHUSDT"][0]["size"]
d = td.desired_copy("ETHUSDT", te, 0.02, tol=0.10)
check("trader +50 (güncel ölçek 0.02) -> 2.0", abs(d-2.0)<1e-9)
td.set_applied("ETHUSDT", d); td.commit_sync("ETHUSDT", te)
# trader AZALTTI 150->75 (yarı) -> applied orantılı yarıya (2.0 -> 1.0)
eff = td.effective_positions([pos("ETHUSDT","LONG",75.0)])
te = [p for p in eff if p["symbol"]=="ETHUSDT"][0]["size"]
d = td.desired_copy("ETHUSDT", te, 0.02, tol=0.10)
check("trader yarıya indi -> orantılı yarı (1.0)", abs(d-1.0)<1e-9)
td.set_applied("ETHUSDT", d); td.commit_sync("ETHUSDT", te)
# küçük değişim (tolerans altı) -> dokunma
eff = td.effective_positions([pos("ETHUSDT","LONG",78.0)])  # ~%4 artış
te = [p for p in eff if p["symbol"]=="ETHUSDT"][0]["size"]
d = td.desired_copy("ETHUSDT", te, 0.02, tol=0.10)
check("tolerans altı değişim -> dokunma (1.0)", abs(d-1.0)<1e-9)

print("\nDELTA: göç (mevcut applied'lı pozisyon, tref yok) -> dokunma, tref sabitlenir:")
tm = CopyTracker(); tm.initialize([pos("BTCUSDT","SHORT",2.0)])
tm.copy("BTCUSDT", [pos("BTCUSDT","SHORT",2.0)])
inst = tm._open_inst("BTCUSDT"); inst["applied"] = 0.05; inst["tref"] = None  # göç simülasyonu
eff = tm.effective_positions([pos("BTCUSDT","SHORT",2.0)])
te = [p for p in eff if p["symbol"]=="BTCUSDT"][0]["size"]
d = tm.desired_copy("BTCUSDT", te, 0.99, tol=0.10)  # faktör absürt olsa bile dokunmamalı
check("göç: mevcut applied korunur (0.05)", abs(d-0.05)<1e-9)
check("göç: tref artık sabit (None değil)", tm._open_inst("BTCUSDT").get("tref") is not None)

print(f"\n{'='*40}\nSONUÇ: {passed} geçti, {failed} kaldı")
sys.exit(1 if failed else 0)
