// Servis çalışanı: veriyi SEKMEDEN BAĞIMSIZ çeker (tab donsa/arka planda olsa bile).
// - csrftoken'ı sayfanın bapi isteklerinden webRequest ile yakalar (kalıcı saklar)
// - chrome.alarms ile periyodik olarak trader verisini çekip yerel bota gönderir
// - sekme açıkken content script'ten gelen mesajla hızlı yol (5 sn) da çalışır

const TRADER = "PASTE_BINANCE_SMART_MONEY_PROFILE_ID";
const BASE = "https://www.binance.com/bapi/asset/v1";
const TOKEN = "PASTE_API_TOKEN_SAME_AS_config.py"; // config.py API_TOKEN ile AYNI olmalı
const EMPTY = "d41d8cd98f00b204e9800998ecf8427e";
let CSRF = "";

chrome.storage.local.get("csrf", (r) => { if (r && r.csrf) CSRF = r.csrf; });

// 1) csrftoken'ı binance bapi isteklerinden yakala
chrome.webRequest.onSendHeaders.addListener(
  (d) => {
    for (const h of d.requestHeaders || []) {
      if (h.name.toLowerCase() === "csrftoken" && h.value && h.value !== EMPTY) {
        if (h.value !== CSRF) { CSRF = h.value; chrome.storage.local.set({ csrf: CSRF }); }
      }
    }
  },
  { urls: ["https://www.binance.com/bapi/*"] },
  ["requestHeaders"]
);

async function getJSON(url) {
  const r = await fetch(url, {
    headers: { clienttype: "web", csrftoken: CSRF, lang: "en-TR" },
    credentials: "include",
  });
  return r.json();
}

async function post(path, body) {
  try {
    await fetch("http://127.0.0.1:8777" + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-TBB-Token": TOKEN },
      body: JSON.stringify(body),
    });
  } catch (e) {}
}
const sendToBot = (payload) => post("/ingest", payload);
const reportBridge = (reason) => post("/bridge", { reason });

// CSRF bayatladığında: bir binance sekmesini sessizce yeniden yükle. Sayfa açılışta bapi
// istekleri atar, webRequest dinleyicisi taze csrftoken'ı yakalar. Çok sık yenilemeyi
// önlemek için cooldown'lı (en fazla ~3 dk'da bir).
let _lastRecover = 0;
async function recoverCsrf() {
  const now = Date.now();
  if (now - _lastRecover < 180000) return;
  _lastRecover = now;
  try {
    const tabs = await chrome.tabs.query({ url: "https://www.binance.com/*" });
    if (tabs && tabs.length) chrome.tabs.reload(tabs[0].id, { bypassCache: false });
  } catch (e) {}
}

async function pull() {
  if (!CSRF) { reportBridge("CSRF yok — Binance sekmesini ziyaret et/yenile"); return; }
  try {
    const pos = await getJSON(
      `${BASE}/private/future/smart-money/profile/query-positions?topTraderId=${TRADER}&marketType=UM&page=1&rows=50`
    );
    if (!pos || !pos.success) {
      // Çoğunlukla CSRF bayatladı (token rotasyona girdi). Bildir + otomatik toparla.
      reportBridge(`Binance reddetti (CSRF bayat?) code=${(pos && pos.code) || "?"}`);
      recoverCsrf();
      return;
    }
    const bal = await getJSON(
      `${BASE}/friendly/future/smart-money/profile/chart-data?topTraderId=${TRADER}&timeRange=30D&chartDataType=BALANCE`
    );
    const items = (bal && bal.data && bal.data.items) || [];
    await sendToBot({ balance: items.length ? items[items.length - 1][1] : 0, positions: pos.data || [] });
  } catch (e) {
    reportBridge("Ağ/fetch hatası: " + (e && e.message ? e.message : "bilinmiyor"));
  }
}

// 2) Sekmeden bağımsız periyodik çekim (alarm SW'yi uyandırır, tab donsa bile çalışır)
chrome.alarms.create("pull", { periodInMinutes: 0.5 }); // ~30 sn (Chrome min)
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "pull") pull(); });
pull(); // SW açılışında hemen bir kez

// 3) Hızlı yol: sekme aktifken content script'ten gelen taze veriyi ilet
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "tbb") sendToBot(msg.payload);
});
