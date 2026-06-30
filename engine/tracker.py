"""
CopyTracker — pozisyon-örneği (instance) bazlı durum takibi + kalıcılık.

Neden instance? Binance smart-money API'si pozisyona benzersiz id vermez. Bir ticker
kapanıp yeniden açıldığında bu "yeni bir pozisyon"dur; eski takip seçimi taşınmamalı.
Bu yüzden her AÇIK pozisyon dönemine kendi kalıcı instance id'mizi atarız ve takip
seçimini ticker'a değil bu instance'a bağlarız.

Kurallar:
  1) Bot açılışındaki pozisyonlar varsayılan TAKİP EDİLMEZ; kullanıcı arayüzden seçer.
     "Takip Et" denince mevcut pozisyon AÇILMAZ; baseline o ana sabitlenir ve yalnızca
     o andan SONRAKİ değişiklikler (büyütme/azaltma/kapatma) oransal kopyalanır.
  2) Bot çalışırken AÇILAN yeni pozisyonlar otomatik takip edilir (tam kopya).
  3) Bir pozisyon kapanırsa instance kapatılır; aynı ticker yeniden açılırsa YENİ instance
     olur (eski takip/baseline taşınmaz).
  4) Takip seçimleri + baseline diske kaydedilir (state_file); restart'ta korunur.
"""

import json
import os


class CopyTracker:
    def __init__(self, state_file=None, auto_follow_new=True):
        self.state_file = state_file
        self.auto_follow_new = auto_follow_new
        self.instances = {}        # iid -> {symbol, side, followed, baseline:[side,size], open, startup}
        self.open_by_symbol = {}   # symbol -> iid (o an açık olan instance)
        self.managed = set()
        self.startup_symbols = set()
        self._counter = 0
        self.initialized = False
        self._load()

    # ---- kalıcılık ----
    def _load(self):
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            d = json.load(open(self.state_file, encoding="utf-8"))
            self.instances = d.get("instances", {})
            self.open_by_symbol = d.get("open_by_symbol", {})
            self.startup_symbols = set(d.get("startup_symbols", []))
            self._counter = d.get("counter", 0)
            self.initialized = d.get("initialized", False)
        except Exception:
            pass

    def _save(self):
        if not self.state_file:
            return
        try:
            json.dump({
                "instances": self.instances, "open_by_symbol": self.open_by_symbol,
                "startup_symbols": sorted(self.startup_symbols),
                "counter": self._counter, "initialized": self.initialized,
            }, open(self.state_file, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception:
            pass

    # ---- instance yönetimi ----
    def _new_iid(self, symbol, side):
        self._counter += 1
        return f"{symbol}-{side}-{self._counter}"

    def _sync(self, positions, first=False):
        """Açık pozisyonları instance'larla eşleştir; yenileri oluştur, kapananları kapat."""
        cur = set()
        for p in positions:
            sym, side = p["symbol"], p["side"]
            cur.add(sym)
            iid = self.open_by_symbol.get(sym)
            inst = self.instances.get(iid) if iid else None
            if inst and inst.get("open") and inst["side"] == side:
                continue  # devam eden aynı pozisyon
            # yeni instance (ilk açılış, yön değişimi veya yeniden açılış)
            auto = (not first) and self.auto_follow_new
            iid = self._new_iid(sym, side)
            self.instances[iid] = {
                "symbol": sym, "side": side,
                "followed": auto,
                "baseline": [side, 0.0] if auto else [side, float(p["size"])],
                "applied": 0.0,   # botun bu pozisyona kendi eklediği kopya miktarı
                "tref": None,     # senkron olunan trader efektif size'ı (delta hesabı için)
                "open": True, "startup": first,
            }
            self.open_by_symbol[sym] = iid
        # listede olmayanları kapat
        for sym, iid in list(self.open_by_symbol.items()):
            if sym not in cur:
                if iid in self.instances:
                    self.instances[iid]["open"] = False
                del self.open_by_symbol[sym]
        self._save()

    def initialize(self, positions):
        if not self.initialized:
            self.startup_symbols = {p["symbol"] for p in positions}
            self.initialized = True
            self._sync(positions, first=True)
        else:
            self._sync(positions)  # restart: kalıcı instance'larla eşleştir

    # ---- takip ----
    def _open_inst(self, symbol):
        return self.instances.get(self.open_by_symbol.get(symbol))

    def follow(self, symbol, positions):
        """TAKİP ET: mevcut pozisyonu AÇMA; baseline o ana sabitlenir, yalnızca bu andan
        sonraki değişiklikler kopyalanır."""
        inst = self._open_inst(symbol)
        if not inst:
            return
        cur = next((p for p in positions if p["symbol"] == symbol), None)
        inst["followed"] = True
        inst["baseline"] = [cur["side"] if cur else inst["side"],
                            float(cur["size"]) if cur else 0.0]
        inst["applied"] = 0.0
        inst["tref"] = None  # bu andan sonraki trader değişimleri güncel ölçekte kopyalanır
        self._save()

    def copy(self, symbol, positions):
        """KOPYALA: trader'ın mevcut pozisyonunu HEMEN ölçekli aç (baseline 0), sonra
        delta takibine devam et."""
        inst = self._open_inst(symbol)
        if not inst:
            return
        cur = next((p for p in positions if p["symbol"] == symbol), None)
        inst["followed"] = True
        inst["baseline"] = [cur["side"] if cur else inst["side"], 0.0]
        inst["applied"] = 0.0
        inst["tref"] = None  # baseline 0 + ilk görüşte trader_eff güncel ölçekte açılır
        self._save()

    def desired_copy(self, symbol, trader_eff, factor, tol=0.10):
        """Botun bu sembolde hedeflediği KENDİ pozisyon boyutu (delta modeli).

        Kural: bot YALNIZCA trader size'ı değişince hareket eder. Değişimi GÜNCEL
        bakiyeye ölçekli uygular; bakiye/PnL değişimi tek başına asla işlem doğurmaz.
          - trader artırırsa  : applied + (artış × güncel faktör)
          - trader azaltırsa  : orantılı azalt (applied × trader_eff/tref)
          - ilk kopya         : trader_eff × güncel faktör
          - mevcut (göç)      : applied korunur (zaten senkron kabul)
        Dönüş: hedef bot boyutu, ya da None (yönetilmiyor).
        """
        inst = self._open_inst(symbol)
        if inst is None:
            return None
        applied = inst.get("applied", 0.0) or 0.0
        tref = inst.get("tref")
        if tref is None:
            if applied > 0:
                # göç/ilk görüş: mevcut pozisyon zaten senkron kabul; tref'i sabitle (emir yok)
                inst["tref"] = trader_eff
                self._save()
                return applied
            return trader_eff * factor  # yeni pozisyon: aç (tref başarıda commit_sync ile gelir)
        if tref > 1e-12 and abs(trader_eff - tref) / tref < tol:
            return applied  # anlamlı değişim yok -> dokunma
        if trader_eff > tref:
            return applied + (trader_eff - tref) * factor   # artış: güncel bakiyeye ölçekli
        if tref > 1e-12:
            return applied * (trader_eff / tref)            # azalış: orantılı
        return trader_eff * factor

    def commit_sync(self, symbol, trader_eff):
        """Emir başarıyla gönderildikten sonra senkron referansını (tref) ilerlet.
        Başarısız emirde çağrılmaz -> bir sonraki döngüde tekrar denenir."""
        inst = self._open_inst(symbol)
        if inst is not None:
            inst["tref"] = trader_eff
            self._save()

    def bot_positions(self):
        """Botun KENDİ kopyaladığı paylar: {symbol: {side, size}} (account pozisyonundan ayrı)."""
        out = {}
        for i in self.instances.values():
            if i.get("open") and (i.get("applied") or 0) > 0:
                out[i["symbol"]] = {"side": i["side"], "size": i["applied"]}
        return out

    def applied_instances(self):
        """applied>0 olan TÜM instance'lar (açık YA DA yeni kapanmış) -> [(symbol, side, applied)].
        Trader bir pozisyonu kapatınca instance open=False olur; yetim kalan kopya payını
        kapatabilmek için kapalıları da döndürürüz."""
        return [(i["symbol"], i["side"], i["applied"])
                for i in self.instances.values() if (i.get("applied") or 0) > 0]

    def clear_applied(self, symbol):
        """Sembole ait TÜM instance'larda applied'i sıfırla (yetim defteri temizleme)."""
        changed = False
        for i in self.instances.values():
            if i["symbol"] == symbol and (i.get("applied") or 0) > 0:
                i["applied"] = 0.0
                changed = True
        if changed:
            self._save()

    def managed_symbols(self):
        """Botun yönettiği semboller: takip edilen + (açık veya hâlâ applied'i olan)."""
        return {i["symbol"] for i in self.instances.values()
                if i.get("followed") and (i.get("open") or (i.get("applied") or 0) > 0)}

    def set_applied(self, symbol, size):
        inst = self._open_inst(symbol)
        if inst is not None:
            inst["applied"] = max(0.0, size)
            self._save()

    def unfollow(self, symbol):
        inst = self._open_inst(symbol)
        if inst:
            inst["followed"] = False
            self._save()

    @property
    def followed(self):
        return {i["symbol"] for i in self.instances.values() if i.get("open") and i.get("followed")}

    # ---- motor verisi ----
    def effective_positions(self, positions):
        self._sync(positions)
        eff = []
        for p in positions:
            inst = self._open_inst(p["symbol"])
            if not inst or not inst["followed"]:
                continue
            b = inst["baseline"]
            if b[0] == p["side"]:
                if p["size"] < b[1]:
                    b[1] = p["size"]
                eff_size = max(0.0, p["size"] - b[1])
            else:
                inst["baseline"] = [p["side"], 0.0]
                eff_size = p["size"]
            if eff_size > 0:
                q = dict(p)
                q["size"] = eff_size
                eff.append(q)
        self._save()
        return eff

    def select_managed_positions(self, eff_positions, my_all_positions):
        candidates = self.managed | {p["symbol"] for p in eff_positions}
        return {s: v for s, v in my_all_positions.items() if s in candidates}

    def update_managed(self, eff_positions, my_managed_positions):
        self.managed = ({p["symbol"] for p in eff_positions}
                        | set(my_managed_positions.keys()))
