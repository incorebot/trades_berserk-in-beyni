// İzole dünyada çalışır: ana dünyaya inject.js'i enjekte eder ve oradan gelen
// veriyi background service worker'a iletir.
const s = document.createElement("script");
s.src = chrome.runtime.getURL("inject.js");
(document.head || document.documentElement).appendChild(s);
s.onload = () => s.remove();

window.addEventListener("message", (ev) => {
  if (ev.source === window && ev.data && ev.data.__tbb) {
    chrome.runtime.sendMessage({ type: "tbb", payload: ev.data.payload });
  }
});
