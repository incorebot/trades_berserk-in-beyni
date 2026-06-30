// Sayfanın ANA dünyasında çalışır: SPA'nın isteklerinden csrftoken'ı yakalar
// (hem fetch hem XHR/axios), trader verisini gerçek oturumla çeker ve content
// script'e gönderir.
(function () {
  const TRADER = "PASTE_BINANCE_SMART_MONEY_PROFILE_ID";
  const EMPTY = "d41d8cd98f00b204e9800998ecf8427e"; // boş/giriş yapılmamış csrf
  const BASE = "https://www.binance.com/bapi/asset/v1";
  let csrf = "";
  let lastLog = 0;

  function note(c) {
    if (c && c !== EMPTY && c !== csrf) {
      csrf = c;
      console.log("[TBB] csrftoken yakalandı ✔");
    }
  }

  // 1) fetch'i sar
  const origFetch = window.fetch;
  window.fetch = function (...args) {
    try {
      const h = args[1] && args[1].headers;
      if (h) note(h.get ? h.get("csrftoken") : (h["csrftoken"] || h["csrfToken"]));
    } catch (e) {}
    return origFetch.apply(this, args);
  };

  // 2) XHR'ı sar (axios çoğu zaman XHR kullanır)
  const origSet = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
    try {
      if (name && name.toLowerCase() === "csrftoken") note(value);
    } catch (e) {}
    return origSet.apply(this, arguments);
  };

  async function getJSON(url) {
    const r = await origFetch(url, {
      headers: { clienttype: "web", csrftoken: csrf, lang: "en-TR" },
      credentials: "include",
    });
    return r.json();
  }

  async function tick() {
    try {
      if (!csrf) {
        if (Date.now() - lastLog > 15000) {
          console.log("[TBB] csrftoken bekleniyor… (Binance sekmesinde gezin/yenile)");
          lastLog = Date.now();
        }
        return;
      }
      const pos = await getJSON(
        `${BASE}/private/future/smart-money/profile/query-positions?topTraderId=${TRADER}&marketType=UM&page=1&rows=50`
      );
      if (!pos || !pos.success) {
        console.log("[TBB] query-positions başarısız:", pos && pos.code);
        return;
      }
      const bal = await getJSON(
        `${BASE}/friendly/future/smart-money/profile/chart-data?topTraderId=${TRADER}&timeRange=30D&chartDataType=BALANCE`
      );
      const items = (bal && bal.data && bal.data.items) || [];
      const payload = {
        balance: items.length ? items[items.length - 1][1] : 0,
        positions: pos.data || [],
      };
      window.postMessage({ __tbb: true, payload }, "*");
      console.log("[TBB] veri gönderildi:", payload.positions.length, "pozisyon");
    } catch (e) {
      console.log("[TBB] hata:", e && e.message);
    }
  }

  console.log("[TBB] köprü aktif, csrftoken yakalanıyor…");
  setInterval(tick, 3000);
  tick();
})();
