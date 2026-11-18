// 网络层录制：把页面发出的播放列表请求连同 Referer 记下来。
//
// 只看 URL 不够——同一个分片域名可能被不同站点引用，下载时必须带上正确的来源页。
// 所以在 onBeforeSendHeaders 里连请求头一起取。

const MAX_PER_TAB = 40;
const key = (tabId) => "cap_" + tabId;

function looksPlaylist(url) {
  const low = url.toLowerCase();
  if (low.startsWith("data:") || low.startsWith("blob:")) return "";
  if (low.includes(".m3u8") || low.includes("format=m3u8")) return "hls";
  if (low.includes(".mpd") || low.includes("format=mpd")) return "dash";
  return "";
}

async function addHit(tabId, url, kind, referer) {
  if (tabId === undefined || tabId < 0) return;
  const k = key(tabId);
  const got = await chrome.storage.session.get(k);
  const cur = got[k] || [];
  if (cur.some((x) => x.url === url)) return;
  cur.push({ url, kind, referer: referer || "", t: Date.now() });
  if (cur.length > MAX_PER_TAB) cur.splice(0, cur.length - MAX_PER_TAB);
  await chrome.storage.session.set({ [k]: cur });
  chrome.action.setBadgeText({ tabId, text: String(cur.length) }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#2a5aa8" }).catch(() => {});
}

// 记录请求头中的 Referer；部分站点的分片请求会带 Origin
chrome.webRequest.onBeforeSendHeaders.addListener(
  (d) => {
    const kind = looksPlaylist(d.url);
    if (!kind) return;
    const h = {};
    (d.requestHeaders || []).forEach((x) => { h[x.name.toLowerCase()] = x.value; });
    addHit(d.tabId, d.url, kind, h.referer || h.origin || "");
  },
  { urls: ["<all_urls>"], types: ["xmlhttprequest", "media", "other", "object", "sub_frame", "main_frame"] },
  ["requestHeaders", "extraHeaders"]
);

// 兜底：只在拿不到请求头时记 URL
chrome.webRequest.onBeforeRequest.addListener(
  (d) => {
    const kind = looksPlaylist(d.url);
    if (kind) addHit(d.tabId, d.url, kind, "");
  },
  { urls: ["<all_urls>"], types: ["xmlhttprequest", "media", "other", "object", "sub_frame", "main_frame"] }
);

chrome.tabs.onRemoved.addListener((tabId) => {
  chrome.storage.session.remove(key(tabId)).catch(() => {});
});

// 页面刷新就清掉上一轮，避免新页面顶着旧地址
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "loading" && info.url) {
    chrome.storage.session.remove(key(tabId)).catch(() => {});
    chrome.action.setBadgeText({ tabId, text: "" }).catch(() => {});
  }
});
