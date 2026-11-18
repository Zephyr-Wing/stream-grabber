// 在页面里挂钩 fetch / XHR，记录请求 URL 与 JSON 响应体。
//
// 为什么需要它：很多站的播放器不自动播放，hls.js 只有在调用 loadSource 之后
// 才会去拉 m3u8。等到用户点开扩展再读 performance，什么都看不到。
// 挂钩之后，只要播放器初始化过（哪怕没播），地址也会被记下来；
// 接口返回的 JSON 里若藏着播放地址，也能捞出来。
//
// 运行在 MAIN world、document_start，先于页面脚本执行。

(() => {
  if (window.__sgHooked) return;
  window.__sgHooked = true;

  const SG = { urls: [], jsons: [] };
  Object.defineProperty(window, "__sg", { value: SG, configurable: true });

  const MAX_URLS = 300;
  const MAX_JSON = 40;
  const JSON_SLICE = 200000;

  function note(u) {
    if (!u || typeof u !== "string") return;
    if (!/^https?:\/\//i.test(u)) return;
    if (SG.urls.indexOf(u) >= 0) return;
    if (SG.urls.length >= MAX_URLS) return;
    SG.urls.push(u);
  }

  function noteJson(text) {
    if (!text || SG.jsons.length >= MAX_JSON) return;
    if (text.indexOf("m3u8") < 0 && text.indexOf("mpd") < 0 &&
        text.indexOf("http") < 0) return;
    SG.jsons.push(String(text).slice(0, JSON_SLICE));
  }

  // ---- fetch
  const rawFetch = window.fetch;
  if (rawFetch) {
    window.fetch = function (input, init) {
      let u = "";
      try {
        u = typeof input === "string" ? input : (input && input.url) || "";
        note(new URL(u, location.href).href);
      } catch (e) {}
      const p = rawFetch.apply(this, arguments);
      try {
        p.then((res) => {
          const ct = (res.headers && res.headers.get("content-type")) || "";
          if (ct.indexOf("json") >= 0 || ct.indexOf("text") >= 0) {
            res.clone().text().then(noteJson).catch(() => {});
          }
        }).catch(() => {});
      } catch (e) {}
      return p;
    };
  }

  // ---- XMLHttpRequest
  const XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype) {
    const rawOpen = XHR.prototype.open;
    const rawSend = XHR.prototype.send;
    XHR.prototype.open = function (method, url) {
      try { this.__sgUrl = new URL(url, location.href).href; note(this.__sgUrl); } catch (e) {}
      return rawOpen.apply(this, arguments);
    };
    XHR.prototype.send = function () {
      try {
        this.addEventListener("load", () => {
          try {
            const ct = this.getResponseHeader("content-type") || "";
            if (ct.indexOf("json") >= 0 || ct.indexOf("text") >= 0) {
              if (typeof this.responseText === "string") noteJson(this.responseText);
            }
          } catch (e) {}
        });
      } catch (e) {}
      return rawSend.apply(this, arguments);
    };
  }
})();
