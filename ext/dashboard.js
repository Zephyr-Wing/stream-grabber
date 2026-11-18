const API = "http://127.0.0.1:8765";
const $ = (id) => document.getElementById(id);

let pending = null;      // popup 传来的候选
let profile = null;      // 当前地址的机制档案
let settings = null;
let dirTarget = "outDir"; // 目录选择器服务于哪个输入框

async function api(path, body, method, ms) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms || 30000);
  try {
    const opt = {
      method: method || (body ? "POST" : "GET"),
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
      signal: ctrl.signal,
    };
    const r = await fetch(API + path, opt);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

function fmtRate(bps) {
  if (!bps) return "";
  if (bps >= 1048576) return (bps / 1048576).toFixed(1) + " MB/s";
  return (bps / 1024).toFixed(0) + " KB/s";
}

function fmtEta(s) {
  if (!s) return "";
  const m = Math.floor(s / 60), r = s % 60;
  return m ? m + " 分 " + r + " 秒" : r + " 秒";
}

function stateText(j) {
  return { queued: "排队", resolving: "解析", downloading: "下载", muxing: "合并",
           fetching: "浏览器取流", done: "完成", failed: "失败",
           paused: "已暂停", cancelled: "已取消" }[j.state] || j.state;
}

// ------------------------------------------------------------ 初始化

async function init() {
  const got = await chrome.storage.local.get("pending");
  pending = got.pending || null;
  await chrome.storage.local.remove("pending");

  let info = null;
  try { info = await api("/v2/ping"); } catch (e) {}
  if (!info || !info.ok) {
    $("status").textContent = "本地服务未连接。运行 server.bat 后刷新本页。";
    $("start").disabled = true;
    return;
  }
  $("status").textContent = "服务已连接 · v" + info.version + " · 保存到 " + info.out_dir;

  settings = (await api("/v2/settings")).settings;
  fillSettings(settings);

  if (pending && pending.items && pending.items.length) {
    const it = pending.items[0];
    $("url").value = it.url || "";
    $("name").value = pending.title || "";
    $("srcHint").textContent = "来自 " + (pending.pageUrl || "");
    await probe();
  } else {
    $("url").value = "";
  }

  $("start").addEventListener("click", startJob);
  $("probe").addEventListener("click", probe);
  $("browse").addEventListener("click", () => openDirs("outDir"));
  $("sBrowse").addEventListener("click", () => openDirs("sOutDir"));
  $("saveSet").addEventListener("click", saveSettings);
  $("dirUp").addEventListener("click", () => loadDirs($("dirPath").dataset.path ? parentOf($("dirPath").dataset.path) : ""));
  $("dirPick").addEventListener("click", pickDir);
  $("modal").addEventListener("click", (e) => { if (e.target.id === "modal") $("modal").classList.remove("on"); });

  pollJobs();
  setInterval(pollJobs, 1000);
}

function parentOf(p) {
  const s = String(p).replace(/[\\/]+$/, "");
  const i = Math.max(s.lastIndexOf("\\"), s.lastIndexOf("/"));
  return i > 0 ? s.slice(0, i) : s;
}

// ------------------------------------------------------------ 探测

async function probe() {
  const url = $("url").value.trim();
  if (!url) return;
  $("probeOut").textContent = "探测中…";
  $("variant").innerHTML = '<option value="">最高可用</option>';
  try {
    const r = await api("/v2/resolve", {
      url, referer: (pending && pending.referer) || "",
      ua: pending ? pending.ua : navigator.userAgent,
      cookies: pending && pending.cookies ? pending.cookies : [],
    });
    profile = r.profile || null;
    if (!profile) { $("probeOut").textContent = "没解析出结果"; return; }
    if (profile.error) {
      $("probeOut").textContent = profile.error;
      return;
    }
    const n = profile.segment_count;
    const bits = ["类型 " + profile.kind];
    if (n > 0) bits.push(n + " 个分片");
    if (profile.master) bits.push("多档");
    if (profile.key && profile.key !== "none") bits.push("加密 " + profile.key);
    if (profile.signed) bits.push("带签名");
    if (profile.cross_domain) bits.push("跨域 CDN");
    bits.push(profile.fetch_route ? "探测经代理" : "探测直连");
    $("probeOut").textContent = bits.join(" · ");

    (profile.variants || []).forEach((v, i) => {
      const o = document.createElement("option");
      o.value = String(i);
      o.textContent = (v.height ? v.height + "p" : (v.name || "档位 " + (i + 1))) +
                      " · " + (v.bandwidth ? (v.bandwidth / 1000).toFixed(0) + " kbps" : "");
      $("variant").appendChild(o);
    });
  } catch (e) {
    $("probeOut").textContent = "探测失败：" + String(e.message || e);
  }
}

// ------------------------------------------------------------ 创建任务

async function startJob() {
  const url = $("url").value.trim();
  if (!url) { $("probeOut").textContent = "先填地址"; return; }

  const vIdx = $("variant").value;
  let variant = {};
  if (profile && vIdx !== "" && profile.variants && profile.variants[Number(vIdx)]) {
    const v = profile.variants[Number(vIdx)];
    variant = { url: v.url, height: v.height, bandwidth: v.bandwidth };
  }

  const payload = {
    url,
    name: $("name").value.trim(),
    out_dir: $("outDir").value.trim(),
    referer: (pending && pending.referer) || "",
    ua: pending ? pending.ua : navigator.userAgent,
    threads: Number($("threads").value) || 16,
    route_mode: $("routeMode").value,
    variant,
    page_mode: !!(profile && profile.kind === "dash"),
  };
  if (pending && pending.cookies) payload.cookies = pending.cookies;
  else payload.cookies = await cookiesFor(url);

  $("start").disabled = true;
  try {
    if ($("fetchMode").value === "browser") {
      await startBrowser(payload);
      return;
    }
    const r = await api("/v2/jobs", payload);
    if (!r.ok) { $("probeOut").textContent = r.error || "提交失败"; return; }
    $("probeOut").textContent = "已加入队列：" + r.job.name;
    pollJobs();
  } finally {
    setTimeout(() => { $("start").disabled = false; }, 800);
  }
}

// 第三级：让原页面里的 content script 逐片取回，服务端只负责落盘与合并
async function startBrowser(payload) {
  const r = await api("/v2/fetch/prepare", payload, "POST", 60000);
  if (!r.ok) {
    $("probeOut").textContent = "无法用浏览器取流：" + (r.error || "准备失败");
    return;
  }
  const plan = r.plan;
  $("probeOut").textContent = "浏览器取流：" + plan.total + " 个分片，正在取…";

  let tabId = pending && pending.tabId;
  if (!tabId && pending && pending.pageUrl) {
    try {
      const tabs = await chrome.tabs.query({ url: pending.pageUrl });
      if (tabs.length) tabId = tabs[0].id;
    } catch (e) {}
  }
  if (!tabId) {
    $("probeOut").textContent = "找不到原页面标签页，请从插件图标重新打开面板";
    return;
  }
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["worker.js"] });
  } catch (e) {
    // 页面已声明 content_scripts，注入失败通常只是因为已存在
  }
  try {
    chrome.tabs.sendMessage(tabId, { type: "sg-fetch", plan }).catch(() => {});
  } catch (e) {
    $("probeOut").textContent = "无法与原页面通信：" + String(e.message || e);
  }
  pollJobs();
}

async function cookiesFor(url) {
  try {
    const list = await chrome.cookies.getAll({ url });
    return list || [];
  } catch (e) {
    return [];
  }
}

// ------------------------------------------------------------ 任务列表

async function pollJobs() {
  let d;
  try { d = await api("/v2/jobs"); } catch (e) { return; }
  if (!d || !d.ok) return;
  const jobs = d.jobs || [];
  $("jobCount").textContent = jobs.length ? jobs.length + " 个" : "";
  const box = $("jobs");
  if (!jobs.length) { box.innerHTML = '<div class="empty">还没有任务</div>'; return; }

  box.textContent = "";
  jobs.slice(0, 12).forEach((j) => {
    const el = document.createElement("div");
    el.className = "job" + (j.state === "done" ? " done" : j.state === "failed" ? " err" : "");

    const head = document.createElement("div");
    head.className = "job-head";
    const nm = document.createElement("span");
    nm.className = "job-name";
    nm.textContent = j.name;
    nm.title = j.url;
    const meta = document.createElement("span");
    meta.className = "job-meta";
    const pct = j.state === "done" ? 100 : (j.progress && j.progress.percent) || 0;
    meta.textContent = stateText(j) + " · " + pct.toFixed(1) + "%" +
      (j.route ? " · " + ({ proxy: "代理", browser: "浏览器", direct: "直连" }[j.route] || j.route) : "") +
      (j.progress && j.progress.rate_bps ? " · " + fmtRate(j.progress.rate_bps) : "") +
      (j.progress && j.progress.eta_sec ? " · 剩 " + fmtEta(j.progress.eta_sec) : "");
    head.append(nm, meta);

    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("i");
    fill.style.width = pct + "%";
    bar.appendChild(fill);

    const msg = document.createElement("div");
    msg.className = "job-msg";
    if (j.state === "done") {
      msg.textContent = (j.size ? (j.size / 1048576).toFixed(1) + " MB → " : "") + (j.output || "");
      msg.title = j.output || "";
    } else if (j.error) {
      msg.textContent = (j.error.code ? "[" + j.error.code + "] " : "") +
                        (j.error.message || "") + (j.error.detail ? " · " + j.error.detail : "");
    } else {
      msg.textContent = j.route_note || "";
    }
    msg.title = msg.textContent;

    const acts = document.createElement("div");
    acts.className = "acts";
    if (j.state === "downloading" || j.state === "queued" || j.state === "resolving") {
      acts.appendChild(btn("暂停", () => act(j.id, "pause")));
    }
    if (j.state === "paused" || j.state === "failed") {
      acts.appendChild(btn("继续", () => act(j.id, "resume")));
    }
    if (j.state === "done") {
      acts.appendChild(btn("打开目录", () => api("/v2/open", { path: j.out_dir }, "POST")));
    }
    if (j.state === "failed" && pending && pending.pageUrl) {
      acts.appendChild(btn("改用浏览器取流", () => retryBrowser(j)));
    }
    if (["downloading", "queued", "paused", "resolving", "fetching"].includes(j.state)) {
      acts.appendChild(btn("取消", () => act(j.id, "cancel")));
    }

    el.append(head, bar, msg);
    if (acts.children.length) el.appendChild(acts);
    box.appendChild(el);
  });
}

function btn(text, fn) {
  const b = document.createElement("button");
  b.textContent = text;
  b.addEventListener("click", fn);
  return b;
}

// 服务端被拒（403 / 签名不符）时，改用浏览器把同一批分片取回来
async function retryBrowser(j) {
  await startBrowser({
    url: j.url,
    name: (j.name || "video") + "_browser",
    out_dir: j.out_dir,
    referer: j.referer || (pending && pending.referer) || "",
    ua: (pending && pending.ua) || "",
    cookies: (pending && pending.cookies) || [],
    threads: 6,
  });
}

async function act(id, action) {
  await api("/v2/jobs/" + id + "/" + action, {});
  setTimeout(pollJobs, 200);
}

// ------------------------------------------------------------ 设置

function fillSettings(s) {
  $("sOutDir").value = s.out_dir || "";
  $("sThreads").value = s.threads || 16;
  $("sRoute").value = s.route_mode || "auto";
  $("sProxy").value = s.proxy || "auto";
  $("outDir").value = s.out_dir || "";
  $("threads").value = s.threads || 16;
  $("routeMode").value = s.route_mode || "auto";
}

async function saveSettings() {
  const payload = {
    out_dir: $("sOutDir").value.trim(),
    threads: Number($("sThreads").value) || 16,
    route_mode: $("sRoute").value,
    proxy: $("sProxy").value.trim() || "auto",
  };
  const r = await api("/v2/settings", payload, "PUT");
  if (r.ok) {
    settings = r.settings;
    fillSettings(settings);
    $("status").textContent = "设置已保存 · 保存到 " + settings.out_dir;
  }
}

// ------------------------------------------------------------ 目录选择

async function openDirs(target) {
  dirTarget = target;
  $("modal").classList.add("on");
  const start = $(target).value.trim();
  await loadDirs(start);
}

async function loadDirs(path) {
  const q = path ? "?path=" + encodeURIComponent(path) : "";
  const d = await api("/v2/dirs" + q);
  if (!d || !d.ok) { $("dirList").innerHTML = '<div class="empty">打不开这个目录</div>'; return; }
  $("dirPath").textContent = d.path;
  $("dirPath").dataset.path = d.path;
  const box = $("dirList");
  box.textContent = "";
  (d.dirs || []).forEach((e) => {
    const row = document.createElement("div");
    row.textContent = "📁 " + e.name;
    row.addEventListener("click", () => loadDirs(e.path));
    box.appendChild(row);
  });
  if (!(d.dirs || []).length) box.innerHTML = '<div class="empty">这个目录下没有子目录</div>';
}

function pickDir() {
  const p = $("dirPath").dataset.path || "";
  if (p) $(dirTarget).value = p;
  $("modal").classList.remove("on");
}

init().catch((e) => { $("status").textContent = "出错了：" + String(e.message || e); });
