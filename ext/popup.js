const API = "http://127.0.0.1:8765";
const HOST = "com.streamgrabber.host";
const $ = (id) => document.getElementById(id);
const ACTIVE = new Set(["queued", "resolving", "downloading", "muxing", "fetching"]);

let tab = null;
let pageTitle = "";
let pageOk = false;      // 当前标签页是不是可抓取的网页
let online = false;
let serverInfo = null;
let busy = false;        // 开关正在启动/停止
let refreshing = false;  // 列表正在加载
let confirmStopAt = 0;   // 有任务在跑时，要求再点一次才停
let watch = null;        // 离线时等服务上线的轮询

async function api(path, body, ms) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms || 25000);
  try {
    const opt = body
      ? { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body), signal: ctrl.signal }
      : { cache: "no-store", signal: ctrl.signal };
    const r = await fetch(API + path, opt);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

function hostOf(u) {
  try { return new URL(u).hostname; } catch (e) { return ""; }
}

function originOf(u) {
  try { return new URL(u).origin + "/"; } catch (e) { return ""; }
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// 扫描所有框架：播放器常常嵌在 iframe 里，只读顶层会什么都看不到
async function scanPage(tabId) {
  const out = { hits: [], title: "", dash: false, error: "" };
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      func: () => {
        const hits = [];
        let dash = false;
        const push = (u, kind) => {
          if (u && /^https?:/i.test(u)) hits.push({ url: u, kind });
        };
        const hints = ["m4s", "videoplayback", "manifest.mpd", "/dash/"];
        for (const e of performance.getEntriesByType("resource")) {
          const u = e.name, low = u.toLowerCase();
          if (low.includes("m3u8")) push(u, "hls");
          else if (low.includes(".mpd")) push(u, "dash");
          else if (hints.some((k) => low.includes(k))) dash = true;
        }
        for (const v of document.querySelectorAll("video,audio")) {
          const s = v.currentSrc || v.src || "";
          const low = s.toLowerCase();
          if (/^https?:/i.test(s)) {
            if (low.includes("m3u8")) push(s, "hls");
            else if (/\.mp4(\?|$)/.test(low)) push(s, "file");
          }
        }
        return { hits, title: document.title, dash };
      },
    });
    for (const r of results || []) {
      const v = r && r.result;
      if (!v) continue;
      out.hits.push(...(v.hits || []));
      if (v.title) out.title = v.title;
      out.dash = out.dash || !!v.dash;
    }
  } catch (e) {
    out.error = String((e && e.message) || e);
  }

  // 再读挂钩记录下来的地址：播放器没自动播时，只有这一层能拿到
  try {
    const hooked = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      world: "MAIN",
      func: () => {
        const sg = window.__sg || { urls: [], jsons: [] };
        const found = [];
        const push = (u, kind) => {
          if (u) found.push({ url: u, kind: kind || "hls" });
        };
        for (const u of sg.urls || []) {
          const low = u.toLowerCase();
          if (low.includes(".m3u8")) push(u, "hls");
          else if (low.includes(".mpd")) push(u, "dash");
          else if (/\.mp4(\?|$)/.test(low)) push(u, "file");
        }
        // 接口 JSON 里常直接带着播放地址
        const re = /https?:\/\/[^\s"'\\<>]+?\.(m3u8|mpd|mp4)(\?[^\s"'\\<>]*)?/gi;
        for (const t of sg.jsons || []) {
          let m;
          const body = String(t).replace(/\\\//g, "/");
          re.lastIndex = 0;
          while ((m = re.exec(body))) {
            const ext = (m[1] || "").toLowerCase();
            push(m[0], ext === "mpd" ? "dash" : ext === "mp4" ? "file" : "hls");
          }
        }
        return found;
      },
    });
    for (const r of hooked || []) {
      const v = r && r.result;
      if (Array.isArray(v)) out.hits.push(...v);
    }
  } catch (e) {}

  return out;
}

async function captured(tabId) {
  try {
    const k = "cap_" + tabId;
    const got = await chrome.storage.session.get(k);
    return got[k] || [];
  } catch (e) {
    return [];
  }
}

function merge(all) {
  const map = new Map();
  for (const h of all) {
    if (!h || !h.url) continue;
    const old = map.get(h.url);
    if (old) {
      if (!old.referer && h.referer) old.referer = h.referer;
      continue;
    }
    map.set(h.url, { url: h.url, kind: h.kind || "hls", referer: h.referer || "" });
  }
  return [...map.values()];
}

function setStatus(state, text) {
  $("dot").className = "dot " + state;
  $("site").textContent = text;
}

function render(items) {
  const box = $("list");
  box.textContent = "";
  $("count").textContent = items.length ? items.length + " 条" : "";
  if (!items.length) return;
  items.forEach((it) => {
    const row = document.createElement("div");
    row.className = "row";

    const best = (it.profile && it.profile.variants && it.profile.variants[0]) || null;
    const badge = document.createElement("span");
    badge.className = "badge" + (best && best.height >= 720 ? " hd" : "");
    if (it.kind === "dash") badge.textContent = "DASH";
    else if (best && best.height) badge.textContent = best.height + "p";
    else badge.textContent = "HLS";

    const seg = document.createElement("span");
    seg.className = "seg";
    const n = it.profile ? it.profile.segment_count : -1;
    seg.innerHTML = n > 0 ? "<b>" + n + "</b> 段" : "<b>—</b>";

    const host = document.createElement("span");
    host.className = "host";
    host.textContent = hostOf(it.url);
    host.title = it.url;

    const btn = document.createElement("button");
    btn.className = "pick";
    btn.textContent = "›";
    btn.title = "在下载面板里配置这一条";
    btn.addEventListener("click", () => openPanel(it));

    row.append(badge, seg, host, btn);
    box.appendChild(row);
  });
}

async function openPanel(item) {
  // item 为 null 表示从「去下载面板」进来，此时没有选中的候选
  const pageUrl = (tab && tab.url) || "";
  const payload = {
    title: pageTitle || (tab ? hostOf(pageUrl) : ""),
    pageUrl: pageUrl,
    referer: (item && item.referer) || originOf(pageUrl),
    items: item ? [item] : [],
    ua: navigator.userAgent,
    tabId: (tab && tab.id) || null,
  };
  await chrome.storage.local.set({ pending: payload });
  await chrome.tabs.create({ url: chrome.runtime.getURL("dashboard.html") });
  window.close();
}

async function cookiesFor(url) {
  try {
    const list = await chrome.cookies.getAll({ url });
    return list || [];
  } catch (e) {
    return [];
  }
}

// ------------------------------------------------------------ 本地服务开关
// 扩展没有启动进程的权限，启动与停止都交给已登记的宿主程序

function nativeCmd(cmd) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(HOST, { cmd }, (res) => {
        const err = chrome.runtime.lastError;
        if (err) {
          resolve({ ok: false, error: err.message });
          return;
        }
        resolve(res || { ok: false, error: "没有返回" });
      });
    } catch (e) {
      resolve({ ok: false, error: String((e && e.message) || e) });
    }
  });
}

function note(text) {
  const el = $("note");
  el.textContent = text || "";
  el.classList.toggle("hidden", !text);
}

async function pingService(ms) {
  try {
    const r = await api("/v2/ping", null, ms || 3000);
    serverInfo = r && r.ok ? r : null;
  } catch (e) {
    serverInfo = null;
  }
  online = !!serverInfo;
  return online;
}

function paintService() {
  const sw = $("svcSwitch");
  sw.classList.toggle("on", online);
  sw.classList.toggle("busy", busy);
  sw.title = busy ? "处理中…" : online ? "点击关闭本地服务" : "点击启动本地服务";

  $("offline").classList.toggle("hidden", online || busy);
  if (!online && !busy) $("offlineText").textContent = "本地服务没开。点右上角开关启动。";

  $("ver").textContent = online && serverInfo ? "v" + serverInfo.version : "";

  if (!pageOk) {
    setStatus(online ? "on" : "off", online ? "本地服务已启动" : "本地服务没开");
  } else {
    setStatus(online ? "on" : "off",
              hostOf(tab.url) + (online ? "" : " · 服务未启动"));
  }
  syncWatch();
}

// 离线时每 2.5 秒探一次：无论是点开关还是去双击 server.bat，服务起来后页面自动接上
function syncWatch() {
  if (online || busy) {
    if (watch) { clearInterval(watch); watch = null; }
    return;
  }
  if (watch) return;
  watch = setInterval(async () => {
    if (busy || refreshing || online) return;
    if (await pingService(1500)) {
      clearInterval(watch);
      watch = null;
      paintService();
      await loadView();
    }
  }, 2500);
}

async function countActiveJobs() {
  try {
    const d = await api("/v2/jobs", null, 4000);
    return ((d && d.jobs) || []).filter((j) => ACTIVE.has(j.state)).length;
  } catch (e) {
    return 0;
  }
}

async function startFlow() {
  busy = true;
  confirmStopAt = 0;
  note("");
  $("offlineHint").textContent = "";
  paintService();
  $("offline").classList.remove("hidden");
  $("offlineText").textContent = "正在启动本地服务…";

  let hostError = null;
  const host = nativeCmd("start");
  host.then((r) => { if (!r.ok) hostError = r.error || "未知错误"; });

  const end = Date.now() + 25000;
  let up = false;
  while (Date.now() < end) {
    if (await pingService(1200)) { up = true; break; }
    if (hostError) break;
    await sleep(350);
  }

  busy = false;
  $("svcSwitch").classList.remove("busy");
  if (up) {
    paintService();
    await loadView();
    return;
  }

  online = false;
  paintService();
  const msg = String(hostError || "");
  $("offlineHint").textContent = msg
    ? (/not found|not installed|Specified native|找不到/i.test(msg)
        ? "启动组件未登记：在项目目录双击 install_host.bat，然后重新加载扩展。"
        : "启动失败：" + msg)
    : "25 秒内没有响应，查看项目下的 .tmp/server.log";
}

async function stopFlow() {
  if (Date.now() > confirmStopAt) {
    const n = await countActiveJobs();
    if (n) {
      confirmStopAt = Date.now() + 5000;
      note("还有 " + n + " 个任务在进行，停止服务会中断它们。再点一次开关确认。");
      return;
    }
  }
  confirmStopAt = 0;
  note("");
  $("offlineHint").textContent = "";
  busy = true;
  paintService();

  const r = await nativeCmd("stop");
  busy = false;
  $("svcSwitch").classList.remove("busy");

  await refresh();
  if (online) {
    note("已发出停止，但服务仍在响应。稍后再点一次，或手动结束进程。");
  } else if (!r.ok) {
    note("停止失败：" + (r.error || "未知错误"));
  }
}

// ------------------------------------------------------------ 页面列表

async function loadView() {
  if (refreshing || !pageOk || !tab) return;
  refreshing = true;
  try {
    const scanned = await scanPage(tab.id);
    if (scanned.title) pageTitle = scanned.title;
    const hits = merge([...(await captured(tab.id)), ...scanned.hits]);

    if (!hits.length) {
      $("count").textContent = "";
      $("list").innerHTML = '<div class="empty">' + (scanned.dash
        ? "这个页面不是 HLS。\n打开下载面板，让 yt-dlp 整页解析。"
        : "还没看到视频流。\n先播放几秒，再点一次插件。") + "</div>";
      return;
    }

    render(hits.map((h) => ({ ...h, profile: null })));
    if (!online) return;

    $("count").textContent = "探测中…";
    // 交给服务端数分片、拆档位：它知道怎么带 Referer 和 cookie
    const cookies = await cookiesFor(tab.url);
    const out = [];
    for (const h of hits) {
      try {
        const r = await api("/v2/resolve", {
          url: h.url, referer: h.referer || originOf(tab.url),
          ua: navigator.userAgent, cookies,
        }, 30000);
        out.push({ ...h, profile: (r && r.profile) || null });
      } catch (e) {
        out.push({ ...h, profile: null });
      }
    }
    out.sort((a, b) => ((b.profile && b.profile.segment_count) || -1) -
                       ((a.profile && a.profile.segment_count) || -1));
    render(out);
  } finally {
    refreshing = false;
  }
}

async function refresh() {
  await pingService();
  paintService();
  await loadView();
}

async function init() {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  tab = t;
  $("panel").onclick = () => openPanel(null);

  if (!tab || !tab.url) {
    $("list").innerHTML = '<div class="empty">拿不到当前标签页</div>';
    await refresh();
    return;
  }
  pageTitle = tab.title || "";

  if (!/^https?:/i.test(tab.url)) {
    $("list").innerHTML = '<div class="empty">换一个有视频的网页再点插件</div>';
    await refresh();
    return;
  }

  pageOk = true;
  await refresh();
}

$("svcSwitch").addEventListener("click", async () => {
  if (busy || refreshing) return;
  if (online) await stopFlow();
  else await startFlow();
});

init().catch((e) => setStatus("off", "出错了：" + String((e && e.message) || e)));
