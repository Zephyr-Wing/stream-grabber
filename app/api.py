# -*- coding: utf-8 -*-
"""本地 HTTP 接口。只监听 127.0.0.1，只接受本机来源。"""
import json
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, jobs, profile as profile_mod
from .fetch import browser as browser_fetch

VERSION = "2.0.0"


class Handler(BaseHTTPRequestHandler):
    server_version = "stream-grabber/%s" % VERSION
    # 客户端异常中断时不至于把这个连接永久挂住
    timeout = 120
    cfg = None
    yt = None
    ff = None

    # ---------------------------------------------------------------- 基础

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _local_only(self):
        ip = self.client_address[0] if self.client_address else ""
        return ip in ("127.0.0.1", "::1")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return {}

    def _raw(self, limit=64 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit:
            return b""
        return self.rfile.read(n)

    def _parts(self):
        u = urllib.parse.urlparse(self.path)
        return u.path.rstrip("/") or "/", urllib.parse.parse_qs(u.query)

    # ---------------------------------------------------------------- GET

    def do_GET(self):
        if not self._local_only():
            return self._json({"ok": False, "error": "local only"}, 403)
        path, q = self._parts()

        if path in ("/", "/v2/ping"):
            return self._json({
                "ok": True, "app": "stream-grabber", "version": VERSION,
                "out_dir": self.cfg["out_dir"], "threads": self.cfg["threads"],
                "route_mode": self.cfg["route_mode"],
                "ytdlp": bool(self.yt), "ffmpeg": bool(self.ff),
                "proxy": config.resolve_proxy(self.cfg),
            })

        if path == "/v2/settings":
            return self._json({"ok": True, "settings": self.cfg})

        if path == "/v2/dirs":
            target = (q.get("path") or [self.cfg["out_dir"]])[0]
            return self._json(_list_dirs(target))

        if path == "/v2/jobs":
            with jobs.LOCK:
                items = [jobs.snapshot(j) for j in
                         sorted(jobs.JOBS.values(), key=lambda x: x["created"], reverse=True)]
            return self._json({"ok": True, "jobs": items})

        if path.startswith("/v2/jobs/"):
            jid = path.split("/")[-1]
            job = jobs.JOBS.get(jid)
            if not job:
                return self._json({"ok": False, "error": "没有这个任务"}, 404)
            return self._json({"ok": True, "job": jobs.snapshot(job)})

        if path == "/v2/events":
            return self._sse()

        return self._json({"ok": False, "error": "not found"}, 404)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()
        end = time.time() + 600
        while time.time() < end:
            with jobs.LOCK:
                items = [jobs.snapshot(j) for j in jobs.JOBS.values()]
            try:
                self.wfile.write(("data: " + json.dumps(
                    {"jobs": items}, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except Exception:
                return
            time.sleep(0.8)

    # ---------------------------------------------------------------- POST/PUT

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if not self._local_only():
            return self._json({"ok": False, "error": "local only"}, 403)
        path, _ = self._parts()
        payload = self._body()

        if path == "/v2/resolve":
            url = (payload.get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                return self._json({"ok": False, "error": "地址格式不对"}, 400)
            proxy = config.resolve_proxy(self.cfg)
            prof = profile_mod.build(url, payload.get("referer"), payload.get("ua"),
                                     payload.get("cookies"), self.cfg,
                                     proxies=[None, proxy] if proxy else [None])
            return self._json({"ok": True, "profile": prof})

        if path == "/v2/jobs":
            r = jobs.create(self.cfg, payload)
            return self._json(r, 200 if r.get("ok") else 400)

        if path == "/v2/fetch/prepare":
            r = browser_fetch.prepare(self.cfg, payload)
            return self._json(r, 200 if r.get("ok") else 400)

        if path.startswith("/v2/fetch/") and path.endswith("/finish"):
            seg = path.split("/")
            if len(seg) == 5:
                r, code = browser_fetch.finish(seg[3], self.cfg, self.ff)
                return self._json(r, code)
        
        if path == "/v2/open":
            target = (payload.get("path") or "").strip()
            if not target or not os.path.isdir(target):
                return self._json({"ok": False, "error": "目录不存在"}, 400)
            if os.name == "nt":
                os.startfile(target)
            return self._json({"ok": True})

        if path.startswith("/v2/jobs/"):
            seg = path.split("/")
            if len(seg) == 5:
                jid, action = seg[3], seg[4]
                fn = {"pause": jobs.pause, "resume": jobs.resume,
                      "cancel": jobs.cancel}.get(action)
                if fn:
                    r = fn(jid)
                    return self._json(r, 200 if r.get("ok") else 404)

        return self._json({"ok": False, "error": "not found"}, 404)

    def do_PUT(self):
        if not self._local_only():
            return self._json({"ok": False, "error": "local only"}, 403)
        path, _ = self._parts()

        # 浏览器取流：分片与密钥以二进制 PUT 上来，必须原样读，不能当 JSON
        if path.startswith("/v2/fetch/"):
            seg = path.split("/")
            if len(seg) == 5:
                data = self._raw()
                if not data:
                    return self._json({"ok": False, "error": "空请求"}, 400)
                r, code = browser_fetch.save_blob(seg[3], seg[4], data)
                return self._json(r, code)

        payload = self._body()
        if path == "/v2/settings":
            for k, v in payload.items():
                if k in config.DEFAULTS:
                    self.cfg[k] = v
            self.cfg["out_dir"] = config._abs(self.cfg["out_dir"])
            self.cfg["tools_dir"] = config._abs(self.cfg["tools_dir"])
            config.save({k: self.cfg[k] for k in config.DEFAULTS})
            return self._json({"ok": True, "settings": self.cfg})

        return self._json({"ok": False, "error": "not found"}, 404)


def _list_dirs(path):
    path = os.path.abspath(path or os.getcwd())
    if not os.path.isdir(path):
        path = os.path.dirname(path) or os.getcwd()
    entries = []
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isdir(full) and not name.startswith("."):
                entries.append({"name": name, "path": full})
    except OSError as e:
        return {"ok": False, "error": str(e), "path": path, "dirs": []}
    parent = os.path.dirname(path)
    return {"ok": True, "path": path, "parent": parent if parent != path else "",
            "dirs": entries}


def serve(cfg, yt, ff, port=None):
    Handler.cfg, Handler.yt, Handler.ff = cfg, yt, ff
    port = int(port or cfg.get("server_port") or 8765)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    return httpd, port
