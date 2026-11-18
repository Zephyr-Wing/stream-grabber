// 第三级取流：在页面上下文里把分片取回来，再交给本地服务。
//
// 为什么要放在页面里：有些 CDN 把出口 IP、会话 cookie 或浏览器指纹签进地址，
// 服务端不论直连还是走代理都会被拒。这里用的是「正在播放这个视频的浏览器」，
// 请求头、cookie、出口都与播放器一致，因此必然能取到。
//
// 只在隔离世界里跑，不碰页面自己的变量；分片逐个 PUT 给 127.0.0.1，
// 由服务端落盘并在最后合并。
(() => {
  if (window.__sgWorkerLoaded) return;
  window.__sgWorkerLoaded = true;

  let busy = false;

  chrome.runtime.onMessage.addListener((msg, _sender, send) => {
    if (!msg || msg.type !== "sg-fetch") return;
    if (busy) {
      send({ ok: false, error: "已有一批在跑" });
      return true;
    }
    run(msg.plan)
      .then((r) => send(r))
      .catch((e) => send({ ok: false, error: String((e && e.message) || e) }));
    return true;  // 异步回包
  });

  async function upload(plan, key, buf) {
    const r = await fetch(plan.api + "/v2/fetch/" + plan.job + "/" + key, {
      method: "PUT",
      headers: { "Content-Type": "application/octet-stream" },
      body: buf,
    });
    if (r.status === 409) throw new Error("stopped");
    if (!r.ok) throw new Error("上传失败 " + r.status);
    return r;
  }

  async function grab(plan, url, key) {
    const r = await fetch(url, { credentials: "include", cache: "no-store" });
    if (!r.ok) throw new Error("取片失败 " + r.status);
    await upload(plan, key, await r.arrayBuffer());
  }

  async function run(plan) {
    busy = true;
    const segs = plan.segments || [];
    const conc = Math.max(1, Math.min(16, plan.concurrency || 6));
    let next = 0;
    let done = 0;
    let failed = 0;
    let stopped = false;

    // 密钥与初始化段先取，缺了后面合并没有意义
    try {
      if (plan.key) await grab(plan, plan.key, "key");
    } catch (e) {}
    try {
      if (plan.map) await grab(plan, plan.map, "map");
    } catch (e) {}

    async function lane() {
      while (!stopped) {
        const i = next++;
        if (i >= segs.length) return;
        try {
          const r = await fetch(segs[i], { credentials: "include", cache: "no-store" });
          if (!r.ok) throw new Error("HTTP " + r.status);
          await upload(plan, i, await r.arrayBuffer());
          done++;
        } catch (e) {
          if (String((e && e.message) || "").includes("stopped")) {
            stopped = true;
            return;
          }
          failed++;
        }
      }
    }

    await Promise.all(Array.from({ length: conc }, lane));
    busy = false;

    // 分片到齐后由这里触发合并，面板关掉也不影响
    let merged = null;
    try {
      const r = await fetch(plan.api + "/v2/fetch/" + plan.job + "/finish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      merged = await r.json();
    } catch (e) {
      merged = { ok: false, error: String((e && e.message) || e) };
    }
    return { ok: !stopped && !!merged && merged.ok !== false,
             done, failed, total: segs.length, stopped, merged };
  }
})();
