const API = "http://127.0.0.1:8765";
const MULTI_SUFFIX = new Set([
  "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
  "co.uk", "ac.uk", "com.au", "co.jp", "com.hk", "com.tw",
]);

let tab = null;
let online = false;
let serverInfo = null;
let pageTitle = "";
const busy = new Set();

const $ = (id) => document.getElementById(id);

async function api(path, body, ms) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms || 20000);
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

// ------------------------------------------------------------ 页面扫描

// performance 里躺着这个页面加载过的所有资源，包括播放器拉的那条 m3u8。
// 比监听网络请求可靠：不受 service worker 休眠影响，打开插件那一刻就在。
async function scanPage(tabId) {
  const out = { hits: [], title: "", href: "", dash: false, error: "" };
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const hits = [], seen = new Set();
        let dash = false;
        const push = (u, kind) => {
          if (!u || !/^https?:/i.test(u) || seen.has(u)) return;
          seen.add(u);
          hits.push({ url: u, kind });
        };
        const DASH_HINT = ["m4s", "videoplayback", "manifest.mpd", "/dash/"];
        for (const e of performance.getEntriesByType("resource")) {
          const u = e.name, low = u.toLowerCase();
          if (low.includes("m3u8")) push(u, "hls");
          else if (low.includes(".mpd")) push(u, "dash");
          else if (DASH_HINT.some((k) => low.includes(k))) dash = true;
        }
        for (const v of document.querySelectorAll("video,audio")) {
          const s = v.currentSrc || v.src || "";
          const low = s.toLowerCase();
          if (/^https?:/i.test(s)) {
            if (low.includes("m3u8")) push(s, "hls");
            else if (/\.mp4(\?|$)/.test(low)) push(s, "file");
          }
        }
        return { hits, title: document.title, href: location.href, dash };
      },
    });
    if (res && res.result) Object.assign(out, res.result);
    else out.error = "这个页面不允许注入脚本";
  } catch (e) {
    out.error = String((e && e.message) || e);
  }
  return out;
}

async function sniffedHits(tabId) {
  try {
    const k = "cap_" + tabId;
    const got = await chrome.storage.session.get(k);
    return got[k] || [];
  } catch (e) {
    return [];
  }
}

function mergeHits(...lists) {
  const map = new Map();
  for (const list of lists) {
    for (const h of list) {
      if (!h || !h.url) continue;
      if (!map.has(h.url)) map.set(h.url, { url: h.url, kind: h.kind || "hls" });
    }
  }
  return [...map.values()];
}

// ------------------------------------------------------------ cookie

async function cookiesFor(url, hostname) {
  const map = new Map();
  const add = (arr) => (arr || []).forEach((c) => {
    if (!c || !c.name) return;
    map.set([c.domain, c.path || "/", c.name].join("|"), c);
  });
  try { add(await chrome.cookies.getAll({ url })); } catch (e) {}
  try { add(await chrome.cookies.getAll({ domain: hostname })); } catch (e) {}

  const parts = hostname.split(".");
  if (parts.length > 2) {
    let base = parts.slice(-2).join(".");
    if (MULTI_SUFFIX.has(base) && parts.length > 3) base = parts.slice(-3).join(".");
    if (!MULTI_SUFFIX.has(base)) {
      try { add(await chrome.cookies.getAll({ domain: base })); } catch (e) {}
    }
  }
  return [...map.values()];
}

// ------------------------------------------------------------ 渲染

function hostOf(u) {
  try { return new URL(u).hostname; } catch (e) { return ""; }
}

function setStatus(state, text) {
  const dot = $("dot");
  dot.className = "dot " + state;
  $("site").textContent = text;
}

function renderList(items, emptyMsg) {
  const box = $("list");
  box.textContent = "";
  $("count").textContent = items.length ? items.length + " 条" : "";

  if (!items.length) {
    const d = document.createElement("div");
    d.className = "row empty";
    d.textContent = emptyMsg;
    box.appendChild(d);
    return;
  }

  items.forEach((it, i) => {
    const row = document.createElement("div");
    row.className = "row";

    const best = (it.variants || [])[0];
    const badge = document.createElement("span");
    badge.className = "badge" + (best && best.height >= 720 ? " hd" : "");
    if (it.kind === "dash") badge.textContent = "DASH";
    else if (best && best.height) badge.textContent = best.height + "p";
    else badge.textContent = "HLS";

    const seg = document.createElement("span");
    seg.className = "seg";
    const n = it.segments;
    seg.innerHTML = n > 0 ? "<b>" + n + "</b> 段" : "<b>?</b>";

    const host = document.createElement("span");
    host.className = "host";
    host.textContent = hostOf(it.url);
    host.title = it.url;

    const dl = document.createElement("button");
    dl.className = "dl";
    dl.textContent = "↓";
    dl.title = "下载这一条";
    dl.addEventListener("click", () => grab(it, i, dl));

    row.append(badge, seg, host, dl);
    box.appendChild(row);
  });
}

function renderJobs(jobs) {
  const box = $("jobs");
  if (!jobs.length) { box.textContent = ""; return; }
  box.textContent = "";

  const show = jobs.slice(0, 3);
  for (const j of show) {
    const div = document.createElement("div");
    div.className = "job " + (j.state === "done" ? "ok" : j.state === "error" ? "err" : "");

    const head = document.createElement("div");
    head.className = "job-head";
    const nm = document.createElement("span");
    nm.className = "job-name";
    nm.textContent = j.name;
    nm.title = j.title || j.name;
    const pct = document.createElement("span");
    pct.className = "job-pct";
    pct.textContent = j.state === "done" ? "完成"
      : j.state === "error" ? "失败"
      : j.state === "queued" ? "排队"
      : (j.percent ? j.percent.toFixed(1) + "%" : "开始…");
    head.append(nm, pct);

    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("i");
    fill.style.width = (j.state === "done" ? 100 : (j.percent || 0)) + "%";
    bar.appendChild(fill);

    const msg = document.createElement("div");
    msg.className = "job-msg" + (j.state === "error" ? " err" : "");
    if (j.state === "done" && j.files && j.files.length) {
      const f = j.files[0];
      msg.textContent = (f.size / 1048576).toFixed(1) + " MB  →  " + f.path;
      msg.title = f.path;
    } else if (j.state === "error") {
      msg.textContent = j.error || "看服务窗口里的输出";
      msg.title = (j.log || []).slice(-3).join("\n");
    } else {
      const last = (j.log || []).filter((l) => l.trim()).slice(-1)[0] || "";
      msg.textContent = last.slice(0, 100);
      msg.title = (j.log || []).slice(-6).join("\n");
    }

    div.append(head, bar, msg);
    box.appendChild(div);
  }
}

// ------------------------------------------------------------ 动作

async function grab(item, index, btn) {
  if (busy.has(item.url)) return;
  busy.add(item.url);
  const old = btn.textContent;
  btn.textContent = "…";
  btn.disabled = true;

  try {
    const cookies = await cookiesFor(tab.url, hostOf(tab.url));
    const name = pageTitle || hostOf(tab.url);
    const r = await api("/api/grab", {
      url: item.url,
      referer: tab.url,
      title: name,
      name: index === 0 ? name : name + "_" + (index + 1),
      cookies,
      ua: navigator.userAgent,
      mode: "hls",
    });
    if (!r.ok) throw new Error(r.error || "提交失败");
    btn.textContent = "✓";
    setTimeout(() => { btn.textContent = old; btn.disabled = false; }, 1400);
    pollJobs();
  } catch (e) {
    btn.textContent = "×";
    btn.title = String(e.message || e);
    setTimeout(() => { btn.textContent = old; btn.disabled = false; }, 2200);
  } finally {
    busy.delete(item.url);
  }
}

async function grabPage(btn) {
  btn.disabled = true;
  const sub = btn.querySelector(".pb-sub");
  const old = sub.textContent;
  sub.textContent = "提交中…";
  try {
    const cookies = await cookiesFor(tab.url, hostOf(tab.url));
    const r = await api("/api/grab", {
      url: tab.url,
      title: pageTitle,
      cookies,
      ua: navigator.userAgent,
      mode: "page",
    });
    if (!r.ok) throw new Error(r.error || "提交失败");
    sub.textContent = "已提交，下载器开始解析";
  } catch (e) {
    sub.textContent = String(e.message || e).slice(0, 80);
  } finally {
    setTimeout(() => { sub.textContent = old; btn.disabled = false; }, 2600);
  }
}

async function pollJobs() {
  if (!online) return;
  try {
    const d = await api("/api/jobs");
    if (d && d.ok) renderJobs(d.jobs || []);
  } catch (e) {
    online = false;
    $("offline").classList.remove("hidden");
    setStatus("off", "本地服务断开了");
  }
}

// ------------------------------------------------------------ 启动

async function init() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  tab = tabs[0];
  if (!tab || !tab.url) { setStatus("off", "拿不到当前页面"); return; }

  pageTitle = tab.title || "";

  const host = hostOf(tab.url);
  if (!/^https?:/i.test(tab.url)) {
    setStatus("off", "浏览器内部页面，没有东西可抓");
    $("pageBtn").disabled = true;
    renderList([], "换一个有视频的网页再点插件");
    return;
  }
  setStatus("", host);

  // 服务在不在
  try {
    serverInfo = await api("/api/ping");
    online = !!(serverInfo && serverInfo.ok);
  } catch (e) {
    online = false;
  }
  if (!online) {
    $("offline").classList.remove("hidden");
    setStatus("off", host + " · 服务未启动");
    $("pageBtn").disabled = true;
  } else {
    setStatus("on", host);
    $("ver").textContent = "v" + serverInfo.version;
    $("offline").classList.add("hidden");
  }

  $("pageBtn").addEventListener("click", (e) => grabPage(e.currentTarget));

  // 两条来源合起来：performance 扫描 + 网络层记录
  const scanned = await scanPage(tab.id);
  if (scanned.title) pageTitle = scanned.title;
  const sniffed = await sniffedHits(tab.id);
  const hits = mergeHits(scanned.hits, sniffed);

  if (scanned.error && !hits.length) {
    renderList([], "没法读这个页面的资源：" + scanned.error);
    return;
  }

  if (!hits.length) {
    if (scanned.dash) {
      // B 站、YouTube 这类：播放器拉的是分片，网络里根本没有 m3u8。
      // 与其让用户对着空列表发呆，不如直接把话说清楚，把主按钮点亮。
      renderList([], "这个页面不是 HLS，网络里没有 m3u8。\n点上方的「下载这个页面」，交给 yt-dlp 解析。");
      $("pageBtn").classList.add("attn");
    } else {
      renderList([], "还没看到视频流。\n先把视频点开播几秒，再点一次插件。");
    }
    if (online) { pollJobs(); setInterval(pollJobs, 1200); }
    return;
  }

  if (!online) {
    // 服务没开也能看到抓到了什么，只是下不了
    renderList(hits.map((h) => ({ ...h, segments: -1, variants: [] })), "");
    return;
  }

  let items = hits.map((h) => ({ ...h, segments: -1, variants: [] }));

  // 先按抓到的原始顺序摆出来，别让人对着空白等；
  // 分片数要一条条去拉，慢的站得几秒。
  renderList(items, "");
  $("count").textContent = "数分片中…";

  try {
    const d = await api("/api/probe", {
      urls: hits.map((h) => ({ url: h.url, referer: tab.url })),
      referer: tab.url,
      ua: navigator.userAgent,
    }, 35000);
    if (d && d.ok && d.items && d.items.length) {
      const kindByUrl = new Map(hits.map((h) => [h.url, h.kind]));
      items = d.items.map((x) => ({ ...x, kind: kindByUrl.get(x.url) || "hls" }));
    }
  } catch (e) {}

  renderList(items, "");
  pollJobs();
  setInterval(pollJobs, 1200);
}

init().catch((e) => {
  setStatus("off", "出错了：" + String((e && e.message) || e).slice(0, 40));
});
