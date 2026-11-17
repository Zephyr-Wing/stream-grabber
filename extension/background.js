// 网络层记录：页面自己发出的请求，这里看得见。
//
// 主力手段其实是 popup 里的 performance 扫描（立刻、不受 service worker
// 休眠影响），这一层只负责补它漏掉的：播放器用 XHR 拼出来的地址、
// Service Worker 转发的请求，performance 有时看不到。

const MAX_PER_TAB = 40;
const key = (tabId) => "cap_" + tabId;

async function addHit(tabId, url, kind) {
  if (tabId === undefined || tabId < 0) return;
  const k = key(tabId);
  const got = await chrome.storage.session.get(k);
  const cur = got[k] || [];
  if (cur.some((x) => x.url === url)) return;
  cur.push({ url, kind, t: Date.now() });
  if (cur.length > MAX_PER_TAB) cur.splice(0, cur.length - MAX_PER_TAB);
  await chrome.storage.session.set({ [k]: cur });
  chrome.action.setBadgeText({ tabId, text: String(cur.length) }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#2a5aa8" }).catch(() => {});
}

chrome.webRequest.onBeforeRequest.addListener(
  (d) => {
    const low = d.url.toLowerCase();
    if (low.startsWith("data:") || low.startsWith("blob:")) return;
    if (low.includes(".m3u8")) addHit(d.tabId, d.url, "hls");
    else if (low.includes(".mpd")) addHit(d.tabId, d.url, "dash");
  },
  {
    urls: ["<all_urls>"],
    types: ["xmlhttprequest", "media", "other", "object", "sub_frame", "main_frame"],
  }
);

chrome.tabs.onRemoved.addListener((tabId) => {
  chrome.storage.session.remove(key(tabId)).catch(() => {});
});

// 页面刷新了就把上一轮的记录清掉，不然新页面会顶着旧地址
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "loading" && info.url) {
    chrome.storage.session.remove(key(tabId)).catch(() => {});
    chrome.action.setBadgeText({ tabId, text: "" }).catch(() => {});
  }
});
