# -*- coding: utf-8 -*-
"""本地下载服务，给浏览器插件用。

插件在浏览器里看到 m3u8 / mpd，把地址、Referer、cookie 一起 POST 过来，
这里起 yt-dlp 干活，进度写进内存，插件轮询着显示。

只监听 127.0.0.1，不对外。启动后最小化就行，不用管它。

    python server.py
    python server.py --port 8765 --no-browser
"""
import argparse
import atexit
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sgrab import config as C
from sgrab import referer as R

VERSION = "1.0"
FMT_PREF = "bv*[vcodec^=avc1]+ba/bv*+ba/b"
UA_FALLBACK = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

JOBS = {}                  # job_id -> dict
QUEUE = []                 # 待跑的 job_id
LOCK = threading.Lock()
WAKE = threading.Condition(LOCK)

MAX_KEEP = 30              # 内存里最多留这么多个任务记录
LOG_LINES = 400


# ---------------------------------------------------------------- 工具函数

def safe_name(s, fallback="video"):
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", (s or "").strip())
    s = s.strip("._")
    if not s:
        s = fallback + "_" + time.strftime("%Y%m%d_%H%M")
    return s[:80]


def cookie_jar(cookies, path):
    """扩展给的 cookie 数组写成 Netscape 格式，喂 yt-dlp --cookies。

    字段名和 DevTools 那份不一样：扩展用 expirationDate / hostOnly，
    这边统一一遍。浏览器自己给的 cookie 是明文的，不碰它的加密库。
    """
    lines = ["# Netscape HTTP Cookie File", "# from stream-grabber extension", ""]
    n = 0
    for c in cookies or []:
        name = (c.get("name") or "").strip()
        domain = (c.get("domain") or "").strip()
        if not name or not domain:
            continue
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = c.get("expirationDate") or c.get("expires") or 0
        try:
            exp = int(float(exp))
        except Exception:
            exp = 0
        dom = ("#HttpOnly_" + domain) if c.get("httpOnly") else domain
        lines.append("\t".join([dom, flag, c.get("path") or "/", secure,
                                str(exp), name, c.get("value") or ""]))
        n += 1
    if not n:
        return None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return path
    except Exception:
        return None


def fetch_text(url, referer=None, ua=None, timeout=8, proxy=None):
    """拉一段文本。

    proxy 为 None 时显式直连，不去读 HTTP_PROXY 这类环境变量：
    探测和下载必须走同一条路，否则会出现「数得到分片、下载 502」这种怪事。
    """
    handlers = [urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", ua or UA_FALLBACK)
    if referer:
        req.add_header("Referer", referer)
    with opener.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def inspect_m3u8(url, referer, ua, proxy=None):
    """看一眼播放列表，数分片、拆清晰度。

    返回 dict：分段数、是否 master、各档清晰度。插件的排序就靠这个：
    正片一千多片，贴片几十片，一眼能分出来。
    """
    out = {"segments": -1, "master": False, "variants": [], "error": ""}
    try:
        txt = fetch_text(url, referer, ua, proxy=proxy)
    except Exception as e:
        out["error"] = str(e)[:120]
        return out
    if "#EXT-X-STREAM-INF" in txt:
        out["master"] = True
        lines = [l.strip() for l in txt.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF"):
                continue
            res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            bw = re.search(r"BANDWIDTH=(\d+)", line)
            name = re.search(r'NAME="([^"]+)"', line)
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if not nxt or nxt.startswith("#"):
                continue
            out["variants"].append({
                "url": urllib.parse.urljoin(url, nxt),
                "width": int(res.group(1)) if res else 0,
                "height": int(res.group(2)) if res else 0,
                "bandwidth": int(bw.group(1)) if bw else 0,
                "name": name.group(1) if name else "",
            })
        out["variants"].sort(key=lambda v: (v["height"], v["bandwidth"]), reverse=True)
        if out["variants"]:
            # 主列表本身没有 #EXTINF，分片数去问最高那档
            try:
                out["segments"] = fetch_text(
                    out["variants"][0]["url"], referer, ua,
                    proxy=proxy).count("#EXTINF")
            except Exception:
                pass
    else:
        out["segments"] = txt.count("#EXTINF")
    return out


# ---------------------------------------------------------------- 下载任务

def _decode(b):
    for enc in ("utf-8", "mbcs", "gbk"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", "replace")


def choose_dl_proxy(cfg, job):
    """下载走直连还是代理。

    auto（默认）直连优先，直连取不到分片才用代理；smart 才两路测速比较。
    整页模式拿不到分片地址，沿用「先代理」的保守选择。
    """
    mode = C.proxy_mode(cfg)
    proxy = job.get("proxy")
    if mode == "off" or not proxy:
        return None
    if mode == "on":
        return proxy
    if job.get("mode") != "hls":
        return proxy
    try:
        from sgrab import route as Route
        # 签名链接多半绑定了取址时的出口 IP，先走同一条路
        if Route.is_signed(job["url"]):
            return proxy
        use_proxy, _, _ = Route.pick(
            job["url"], job.get("referer") or "",
            job.get("ua") or UA_FALLBACK, proxy, smart=(mode == "smart"))
        return use_proxy
    except Exception:
        return None


def build_cmd(cfg, yt, ff, job):
    url = job["url"]
    out_tmpl = os.path.join(job["out_dir"], job["name"] + ".%(ext)s")
    extra = []
    if ff:
        extra += ["--ffmpeg-location", ff]
    if job.get("dl_proxy"):
        extra += ["--proxy", job["dl_proxy"]]
    if job.get("jar"):
        extra += ["--cookies", job["jar"]]

    common = ["--newline", "--no-warnings",
              "--retries", str(cfg["retries"]),
              "--socket-timeout", str(cfg["socket_timeout"]),
              "-o", out_tmpl] + extra

    if job["mode"] == "page":
        # 整页交给 yt-dlp 解析器时，别硬塞 Referer / UA，有些站鉴权会坏
        return [yt, url, "-f", FMT_PREF, "--merge-output-format", "mp4",
                "--no-playlist"] + common

    ref = job.get("referer") or ""
    if not ref:
        ref = R.find(url, cfg.get("referer_map")) or ""
    h = R.host_of(url)
    ref = ref or ("https://" + h + "/" if h else "")
    cmd = [yt, url]
    if ref:
        cmd += ["--referer", ref]
    cmd += ["--user-agent", job.get("ua") or UA_FALLBACK,
            "-N", str(cfg["threads"]),
            "--fragment-retries", str(cfg["fragment_retries"])]
    return cmd + common


def run_job(cfg, yt, ff, job):
    job["state"] = "running"
    job["started"] = time.time()
    print("[任务] %s  开始" % job["name"])
    job["dl_proxy"] = choose_dl_proxy(cfg, job)
    cmd = build_cmd(cfg, yt, ff, job)
    job["cmd"] = " ".join('"%s"' % c if " " in c else c for c in cmd[:6]) + " …"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")

    def push(line):
        line = line.rstrip()
        if not line:
            return
        job["log"].append(line)
        if len(job["log"]) > LOG_LINES:
            del job["log"][:len(job["log"]) - LOG_LINES]
        m = re.search(r"(\d+\.\d+)%", line)
        if m:
            job["percent"] = float(m.group(1))
        m = re.search(r"of\s+~?\s*([\d.]+)(KiB|MiB|GiB)", line)
        if m:
            job["size"] = m.group(1) + " " + m.group(2)

    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=env,
                             creationflags=(subprocess.CREATE_NO_WINDOW
                                            if os.name == "nt" else 0))
        for raw in iter(p.stdout.readline, b""):
            push(_decode(raw))
        p.wait()
        rc = p.returncode
    except Exception as e:
        job["state"] = "error"
        job["error"] = "%s: %s" % (type(e).__name__, e)
        return
    finally:
        if job.get("jar"):
            try:
                os.remove(job["jar"])
            except OSError:
                pass

    found = []
    try:
        for f in os.listdir(job["out_dir"]):
            if not f.startswith(job["name"] + "."):
                continue
            if ".part" in f or ".ytdl" in f:
                continue
            fp = os.path.join(job["out_dir"], f)
            if os.path.isfile(fp) and os.path.getsize(fp) > 10240:
                found.append((f, os.path.getsize(fp)))
    except OSError:
        pass

    if found:
        job["state"] = "done"
        job["percent"] = 100.0
        job["files"] = [{"name": f, "size": s,
                         "path": os.path.join(job["out_dir"], f)}
                        for f, s in found]
        tot = sum(s for _, s in found) / 1048576.0
        print("[任务] %s  完成  %.1f MB" % (job["name"], tot))
    elif rc in (130, 143, -2, -15):
        job["state"] = "error"
        job["error"] = "已中断"
        print("[任务] %s  中断" % job["name"])
    else:
        job["state"] = "error"
        # 复探一次，把 403 翻译成具体原因（签名/IP、Referer、过期…）
        from sgrab import route as Route
        code, lines = Route.explain_failure(
            job["url"], job.get("referer") or "", job.get("dl_proxy"))
        for line in lines:
            job["log"].append(line)
            print("        " + line)
        job["error"] = lines[0] if lines else ("yt-dlp 退出码 %s" % rc)
        print("[任务] %s  失败（退出码 %s）" % (job["name"], rc))
    job["finished"] = time.time()


def worker(cfg, yt, ff):
    """串行跑。同时开两个任务只会互相抢带宽，还可能撞 CDN 限流。"""
    while True:
        with WAKE:
            while not QUEUE:
                WAKE.wait()
            job = JOBS.get(QUEUE.pop(0))
        if not job:
            continue
        run_job(cfg, yt, ff, job)


def submit(cfg, payload):
    yt = C.find_ytdlp(cfg)
    if not yt:
        return {"ok": False, "error": "找不到 yt-dlp，先跑 python setup_tools.py"}
    ff = C.find_ffmpeg(cfg)

    url = (payload.get("url") or "").strip()
    if not re.match(r"https?://", url, re.I):
        return {"ok": False, "error": "地址要以 http(s):// 开头"}

    title = (payload.get("title") or "").strip()
    name = safe_name(payload.get("name") or title or "video")
    out_dir = C.ensure_out(cfg)
    job_id = uuid.uuid4().hex[:12]

    jar = None
    cookies = payload.get("cookies") or []
    if cookies:
        jar = cookie_jar(cookies, os.path.join(
            os.path.dirname(os.path.abspath(__file__)), ".tmp", "ck_%s.txt" % job_id))

    job = {
        "id": job_id,
        "url": url,
        "title": title or name,
        "name": name,
        "out_dir": out_dir,
        "mode": payload.get("mode") or "hls",
        "referer": (payload.get("referer") or "").strip(),
        "ua": (payload.get("ua") or "").strip(),
        "proxy": payload.get("proxy") or C.resolve_proxy(cfg),
        "jar": jar,
        "state": "queued",
        "percent": 0.0,
        "size": "",
        "log": [],
        "files": [],
        "error": "",
        "created": time.time(),
    }
    if jar:
        atexit.register(lambda p=jar: os.path.exists(p) and os.remove(p))

    with LOCK:
        JOBS[job_id] = job
        QUEUE.append(job_id)
        WAKE.notify()
        # 老的清掉，内存别越堆越多
        if len(JOBS) > MAX_KEEP:
            for k in sorted(JOBS, key=lambda x: JOBS[x]["created"])[:len(JOBS) - MAX_KEEP]:
                JOBS.pop(k, None)

    return {"ok": True, "id": job_id, "name": name,
            "out": os.path.join(out_dir, name + ".mp4"),
            "logged_in": bool(jar)}


def job_view(job, tail=60):
    d = {k: v for k, v in job.items() if k not in ("log", "jar", "out_dir")}
    d["log"] = job["log"][-tail:]
    return d


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "stream-grabber/%s" % VERSION
    cfg = None
    yt = None
    ff = None

    def log_message(self, fmt, *args):
        pass                      # 别刷屏，控制台只留自己的输出

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        # 某些 Chrome 版本对「非安全上下文访问 localhost」要这个头才放行
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _local_only(self):
        """只认本机。插件是本地跑的，远程请求一律不理。"""
        ip = self.client_address[0] if self.client_address else ""
        return ip in ("127.0.0.1", "::1")

    def do_GET(self):
        if not self._local_only():
            return self._json({"ok": False, "error": "local only"}, 403)
        path = urllib.parse.urlparse(self.path).path

        if path in ("/", "/api/ping"):
            return self._json({
                "ok": True, "app": "stream-grabber", "version": VERSION,
                "out_dir": self.cfg["out_dir"],
                "threads": self.cfg["threads"],
                "ytdlp": bool(self.yt),
                "ffmpeg": bool(self.ff),
            })

        if path == "/api/jobs":
            with LOCK:
                items = [job_view(j, tail=12) for j in
                         sorted(JOBS.values(), key=lambda x: x["created"], reverse=True)]
            return self._json({"ok": True, "jobs": items})

        if path == "/api/job":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            jid = (q.get("id") or [""])[0]
            job = JOBS.get(jid)
            if not job:
                return self._json({"ok": False, "error": "没有这个任务"}, 404)
            return self._json({"ok": True, "job": job_view(job, tail=80)})

        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if not self._local_only():
            return self._json({"ok": False, "error": "local only"}, 403)
        path = urllib.parse.urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as e:
            return self._json({"ok": False, "error": "请求体不是 JSON：%s" % e}, 400)

        if path == "/api/probe":
            # 插件把候选地址一股脑发来，这里数分片、拆清晰度，替它排好序。
            # 并发探：一个页面常常挂着好几条候选，串着拉会把插件的请求拖到超时。
            ua = payload.get("ua") or UA_FALLBACK
            ref = payload.get("referer") or ""
            # 播放列表只有几百字节，稳妥优先；下载路径单独按实测决定
            proxy = C.probe_proxy(self.cfg)
            targets = []
            for it in (payload.get("urls") or [])[:20]:
                u = it.get("url") if isinstance(it, dict) else it
                if not u:
                    continue
                r = it.get("referer") if isinstance(it, dict) else None
                targets.append((u, r or ref))

            def one(item):
                u, r = item
                info = inspect_m3u8(u, r, ua, proxy=proxy)
                info["url"] = u
                return info

            out = []
            if targets:
                with ThreadPoolExecutor(max_workers=6) as ex:
                    out = list(ex.map(one, targets))

            # 分片多的排前面：正片一千多，贴片几十
            out.sort(key=lambda x: (x["segments"], max(
                [v["height"] for v in x["variants"]] or [0])), reverse=True)
            return self._json({"ok": True, "items": out})

        if path == "/api/grab":
            r = submit(self.cfg, payload)
            return self._json(r, 200 if r.get("ok") else 400)

        return self._json({"ok": False, "error": "not found"}, 404)


def main():
    C.setup_console()
    ap = argparse.ArgumentParser(prog="server.py")
    ap.add_argument("-p", "--port", type=int, default=0, help="默认读配置，8765")
    ap.add_argument("-c", "--config", default="")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开下载目录")
    args = ap.parse_args()

    cfg = C.load(args.config or None)
    port = args.port or int(cfg.get("server_port") or 8765)

    # 已经有一个在跑就别再起一个，直接说一声
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open("http://127.0.0.1:%d/api/ping" % port, timeout=1) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("app") == "stream-grabber":
            print("服务已经在跑了（端口 %d，版本 %s）。" % (port, d.get("version")))
            print("直接去浏览器里点插件图标就行。")
            if not args.no_browser:
                os.startfile(cfg["out_dir"]) if os.name == "nt" else None
            return 0
    except Exception:
        pass

    yt = C.find_ytdlp(cfg)
    ff = C.find_ffmpeg(cfg)
    Handler.cfg, Handler.yt, Handler.ff = cfg, yt, ff

    if not yt:
        print("[!] 找不到 yt-dlp，插件会收到 400。先跑 python setup_tools.py")
    if not ff:
        print("[!] 找不到 ffmpeg，音视频分离的站会合并不了。")

    threading.Thread(target=worker, args=(cfg, yt, ff), daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True

    print("=" * 58)
    print(" stream-grabber 本地服务  v%s" % VERSION)
    print("=" * 58)
    print(" 监听    127.0.0.1:%d（只对本机开放）" % port)
    print(" 保存到  %s" % cfg["out_dir"])
    print(" 线程    %d" % cfg["threads"])
    print()
    print(" 现在去浏览器点插件图标：看到地址就选一条，不选就下整页。")
    print(" 这个窗口最小化留着，关掉插件就用不了了。")
    print("=" * 58)
    print()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
