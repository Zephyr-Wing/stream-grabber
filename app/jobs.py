# -*- coding: utf-8 -*-
"""任务状态机与执行线程。

一个任务从创建到出片：
  queued → resolving → downloading → done / failed / paused / cancelled

暂停 = 终止 yt-dlp 进程，保留 .part；继续 = 重新入队，由 yt-dlp 续传。
"""
import os
import subprocess
import threading
import time
import uuid

from . import config, net, profile as profile_mod, route as route_mod
from .fetch import local

JOBS = {}
QUEUE = []
LOCK = threading.Lock()
WAKE = threading.Condition(LOCK)
MAX_KEEP = 50
LOG_LINES = 400

# 下载看门狗：直连跑起来后如果速率长期过低，换代理再试一次。
# 单连接探测看不出真实吞吐（分片 CDN 按连接限速），只有实际下载的速率算数。
WATCH_AFTER = 25
SLOW_FLOOR = 150 * 1024

ERROR_ACTIONS = {
    "E_DRM": "该内容受 DRM 保护，无法下载",
    "E_SIGN_IP": "签名与出口不匹配：已试过直连与代理；检查代理是否按进程分流",
    "E_REFERER": "防盗链拒绝了请求：已用页面根域补 Referer，仍被拒",
    "E_EXPIRED": "地址已过期：回到页面重新播放几秒，再点一次下载",
    "E_NO_STREAM": "页面上没有可用的视频流：先让播放器加载起来",
    "E_NET": "网络不可达：检查代理与网络后重试",
    "E_TOOL": "缺少 yt-dlp 或 ffmpeg：放进 tools/ 或系统 PATH 后重启服务",
}


def _new_id():
    return uuid.uuid4().hex[:12]


def snapshot(job):
    d = {k: v for k, v in job.items() if not k.startswith("_")}
    d["log"] = job["_log"][-60:]
    # 分片清单动辄上千条，面板每秒轮询不该把它带上
    prof = d.get("profile")
    if isinstance(prof, dict):
        d["profile"] = {k: v for k, v in prof.items()
                        if k not in ("segments", "durations")}
    return d


def create(cfg, payload):
    job = _mk_job(cfg, payload)
    if not job:
        return {"ok": False, "error": "地址需要以 http(s):// 开头"}
    _enqueue(job)
    return {"ok": True, "job": snapshot(job)}


def register(cfg, payload):
    """登记一个不由本进程下载的任务（例如浏览器取流），只进列表不进队列。"""
    job = _mk_job(cfg, payload)
    if not job:
        return None
    with LOCK:
        JOBS[job["id"]] = job
        _trim()
    return job


def log(job, line):
    _log(job, line)


def fail(job, code, detail=""):
    _fail(job, code, detail)


def _trim():
    if len(JOBS) > MAX_KEEP:
        for k in sorted(JOBS, key=lambda x: JOBS[x]["created"])[:len(JOBS) - MAX_KEEP]:
            JOBS.pop(k, None)


def _enqueue(job):
    with LOCK:
        JOBS[job["id"]] = job
        QUEUE.append(job["id"])
        WAKE.notify()
        _trim()


def _mk_job(cfg, payload):
    url = (payload.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None

    name = (payload.get("name") or "").strip() or ("video_" + time.strftime("%Y%m%d_%H%M"))
    return {
        "id": _new_id(),
        "url": url,
        "name": name,
        "out_dir": config.ensure_out(cfg, payload.get("out_dir")),
        "referer": (payload.get("referer") or "").strip(),
        "ua": (payload.get("ua") or "").strip(),
        "cookies": payload.get("cookies") or [],
        "variant": payload.get("variant") or {},
        "threads": int(payload.get("threads") or cfg["threads"]),
        "route_mode": payload.get("route_mode") or cfg["route_mode"],
        "page_mode": bool(payload.get("page_mode")),
        "state": "queued",
        "stage": "queued",
        "route": "",
        "route_note": "",
        "proxy": None,
        "profile": None,
        "progress": {"percent": 0.0, "rate_bps": 0.0, "eta_sec": 0,
                     "total_text": "", "segments_done": 0, "segments_total": -1},
        "error": None,
        "output": "",
        "created": time.time(),
        "started": 0,
        "finished": 0,
        "_log": [],
        "_proc": None,
        "_pause": False,
        "_cancel": False,
        "_switch_to": None,
        "_switched": False,
        "_jar": None,
    }


def _stop_proc(proc):
    """结束整棵进程树。

    yt-dlp.exe 是 PyInstaller 打包的单文件程序，有一个 bootloader 父进程；
    只 terminate 父进程的话，真正干活的子进程会变成孤儿继续下载。
    """
    if not proc or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def pause(jid):
    job = JOBS.get(jid)
    if not job:
        return {"ok": False, "error": "没有这个任务"}
    job["_pause"] = True
    # 记住点击那一刻的进度：管道里还有已写好的行，收尾时按这个值还原
    job["_pause_percent"] = job["progress"]["percent"]
    _stop_proc(job.get("_proc"))
    if not job.get("_proc"):
        job["state"] = "paused"
        job["stage"] = "paused"
    return {"ok": True, "job": snapshot(job)}


def resume(jid):
    job = JOBS.get(jid)
    if not job:
        return {"ok": False, "error": "没有这个任务"}
    job["_pause"] = False
    job["state"] = "queued"
    job["stage"] = "queued"
    with LOCK:
        if jid not in QUEUE:
            QUEUE.append(jid)
        WAKE.notify()
    return {"ok": True, "job": snapshot(job)}


def cancel(jid):
    job = JOBS.get(jid)
    if not job:
        return {"ok": False, "error": "没有这个任务"}
    job["_cancel"] = True
    _stop_proc(job.get("_proc"))
    job["state"] = "cancelled"
    job["stage"] = "cancelled"
    return {"ok": True, "job": snapshot(job)}


def _log(job, line):
    job["_log"].append(line.rstrip())
    if len(job["_log"]) > LOG_LINES:
        del job["_log"][:len(job["_log"]) - LOG_LINES]


def _fail(job, code, detail=""):
    job["state"] = "failed"
    job["stage"] = "failed"
    job["error"] = {"code": code, "message": ERROR_ACTIONS.get(code, "下载失败"),
                    "detail": detail[:300]}
    job["finished"] = time.time()
    print("[任务] %s 失败 %s %s" % (job["id"], code, detail[:120]))


def _cleanup(job):
    jar = job.get("_jar")
    if jar and os.path.exists(jar):
        try:
            os.remove(jar)
        except OSError:
            pass
    job["_jar"] = None


def _resolve(job, cfg, yt, ff):
    job["state"] = "resolving"
    job["stage"] = "resolve"

    proxy = config.resolve_proxy(cfg)
    prof = profile_mod.build(job["url"], job["referer"], job["ua"], job["cookies"],
                             cfg, proxies=[None, proxy] if proxy else [None])
    job["profile"] = prof

    if prof.get("error"):
        code = "E_DRM" if prof.get("kind") == "drm" else "E_NO_STREAM"
        _fail(job, code, prof["error"])
        return False

    if prof.get("kind") == "dash":
        # DASH 交给 yt-dlp 整页解析，不做分片探测
        job["page_mode"] = True
        job["route"] = "proxy" if proxy else "direct"
        job["proxy"] = proxy
        job["route_note"] = "DASH：交给 yt-dlp 解析"
        return True

    picked = route_mod.choose(cfg, prof, job["ua"], job["cookies"], proxy)
    if not picked:
        code = "E_SIGN_IP" if (prof.get("signed") or prof.get("ip_bound")) else "E_REFERER"
        _fail(job, code, "直连与代理都取不到分片")
        return False

    job["route"] = picked["route"]
    job["proxy"] = picked["proxy"]
    job["route_note"] = picked["note"]
    job["progress"]["segments_total"] = prof.get("segment_count", -1)
    job["referer"] = job["referer"] or prof.get("page_origin") or ""
    return True


def _run_direct(job):
    from .fetch import direct

    def on_progress(pct, rate, eta, done, total):
        p = job["progress"]
        p["percent"] = pct
        p["rate_bps"] = rate
        p["eta_sec"] = eta
        p["bytes"] = done
        p["total_text"] = ("%.1f MB" % (total / 1048576.0)) if total else ""

    status, info = direct.download(
        job, on_progress, lambda: job["_pause"] or job["_cancel"])

    if status == "done":
        job["state"] = "done"
        job["stage"] = "done"
        job["output"] = info
        try:
            job["size"] = os.path.getsize(info)
        except OSError:
            job["size"] = 0
        job["progress"]["percent"] = 100.0
        job["finished"] = time.time()
        print("[任务] %s 完成 %s" % (job["id"], os.path.basename(info)))
    elif status == "paused":
        job["state"] = "paused"
        job["stage"] = "paused"
        at = job.get("_pause_percent")
        if at is not None:
            job["progress"]["percent"] = at
        _log(job, "已暂停，断点已保留")
    elif status == "cancelled":
        job["state"] = "cancelled"
        job["stage"] = "cancelled"
        job["finished"] = time.time()
    else:
        prof = job.get("profile") or {}
        _fail(job, "E_SIGN_IP" if prof.get("signed") else "E_NET", info)


def _watch_slow(job, proc, alt_proxy):
    """下载实际跑起来后盯着速率；直连长期过慢就标记改走代理。"""
    start = time.time()
    while proc.poll() is None:
        time.sleep(4)
        if job["_switch_to"] or job["_pause"] or job["_cancel"]:
            return
        if time.time() - start < WATCH_AFTER:
            continue
        rate = job["progress"].get("rate_bps") or 0
        if rate and rate < SLOW_FLOOR:
            job["_switch_to"] = alt_proxy
            _log(job, "直连只有 %.0f KB/s，改走代理重试（已下部分会续传）"
                 % (rate / 1024.0))
            _stop_proc(proc)
            return


def run_job(cfg, yt, ff, job):
    job["started"] = time.time()
    if not _resolve(job, cfg, yt, ff):
        _cleanup(job)
        return

    if not yt:
        _fail(job, "E_TOOL", "找不到 yt-dlp")
        _cleanup(job)
        return

    job["state"] = "downloading"
    job["stage"] = "download"

    # 单个媒体文件：自己下，比让 yt-dlp 先当网页抓一遍快得多
    if (job.get("profile") or {}).get("kind") == "direct":
        _run_direct(job)
        return

    if job["cookies"]:
        jar = local.cookie_jar(job["cookies"],
                               os.path.join(config.ROOT, ".tmp", "ck_%s.txt" % job["id"]))
        job["_jar"] = jar
    job["_jar"] = job.get("_jar") or None

    def on_line(text):
        _log(job, text)
        # 暂停/取消后管道里还堆着已写好的进度行，继续解析会让进度凭空跳到后面
        if job["_pause"] or job["_cancel"]:
            return
        local.parse_line(text, job)
        if "%" in text:
            job["progress"]["segments_done"] = int(
                job["progress"]["percent"] * max(job["progress"]["segments_total"], 1) / 100)

    alt_proxy = config.resolve_proxy(cfg) if (cfg.get("route_mode") or "auto") == "auto" else None
    rc = 0
    while True:
        proc = local.run(job, cfg, yt, ff, on_line)
        job["_proc"] = proc
        if alt_proxy and not job.get("proxy"):
            threading.Thread(target=_watch_slow, args=(job, proc, alt_proxy),
                             daemon=True).start()
        for raw in iter(proc.stdout.readline, b""):
            on_line(local._decode(raw))
        proc.wait()
        rc = proc.returncode
        job["_proc"] = None

        if job["_switch_to"]:
            job["proxy"] = job["_switch_to"]
            job["route"] = "proxy"
            job["route_note"] = "直连过慢，已改走代理（续传）"
            job["_switch_to"] = None
            job["_switched"] = True
            continue
        break

    if job["_cancel"]:
        job["state"] = "cancelled"
        job["stage"] = "cancelled"
        job["finished"] = time.time()
        _cleanup(job)
        return

    if job["_pause"]:
        job["state"] = "paused"
        job["stage"] = "paused"
        at = job.get("_pause_percent")
        if at is not None:
            job["progress"]["percent"] = at
        _log(job, "已暂停，断点已保留")
        _cleanup(job)
        return

    found = []
    try:
        for f in os.listdir(job["out_dir"]):
            if not f.startswith(job["name"] + "."):
                continue
            if ".part" in f or ".ytdl" in f:
                continue
            p = os.path.join(job["out_dir"], f)
            if os.path.isfile(p) and os.path.getsize(p) > 10240:
                found.append((f, os.path.getsize(p)))
    except OSError:
        pass

    if found:
        job["state"] = "done"
        job["stage"] = "done"
        job["progress"]["percent"] = 100.0
        job["output"] = os.path.join(job["out_dir"], found[0][0])
        job["size"] = found[0][1]
        job["finished"] = time.time()
        print("[任务] %s 完成 %s" % (job["id"], found[0][0]))
    else:
        # 用首片再探一次，把失败翻译成错误码
        _diagnose(job, cfg, rc)
    _cleanup(job)


def _diagnose(job, cfg, rc):
    prof = job.get("profile") or {}
    seg = prof.get("segment_base") or job["url"]
    referer = prof.get("page_origin") or job["referer"]
    ok, got, code, why = net.probe_first_chunk(
        seg, referer, job["ua"], job["proxy"], job["cookies"], need=8192)
    if ok:
        _fail(job, "E_NET", "分片地址可访问，但下载未完成（退出码 %s）" % rc)
    elif code in (401, 403):
        signed = prof.get("signed") or prof.get("ip_bound")
        _fail(job, "E_SIGN_IP" if signed else "E_REFERER", why or ("HTTP %s" % code))
    elif code == 404:
        _fail(job, "E_EXPIRED", "分片返回 404")
    else:
        _fail(job, "E_NET", why or ("HTTP %s" % code))


def worker(cfg, yt, ff):
    while True:
        with WAKE:
            while not QUEUE:
                WAKE.wait()
            jid = QUEUE.pop(0)
        job = JOBS.get(jid)
        if not job:
            continue
        if job["state"] in ("cancelled", "done"):
            continue
        try:
            run_job(cfg, yt, ff, job)
        except Exception as e:
            _fail(job, "E_NET", "%s: %s" % (type(e).__name__, e))


def start(cfg):
    yt = config.find_ytdlp(cfg)
    ff = config.find_ffmpeg(cfg)
    t = threading.Thread(target=worker, args=(cfg, yt, ff), daemon=True)
    t.start()
    return yt, ff
