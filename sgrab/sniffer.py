# -*- coding: utf-8 -*-
"""用真实浏览器把页面里的视频流地址「嗅」出来。

为什么不能直接用 HTTP 请求页面：
  1. 这类站点普遍挂了 Cloudflare 人机验证，命令行拿到的只是挑战页；
  2. 播放地址由 JS 动态下发，页面源码里没有静态链接；
  3. 播放器先放一段贴片广告，真正的正片要等播放器初始化后才请求。

做法：启动一个独立配置的浏览器（不碰你自己的日常配置），
通过 DevTools 协议监听所有网络请求，自动点击播放，再挑出「分片最多」的那条
播放列表 —— 正片通常上千个分片，广告只有几十个。
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def free_port(preferred=9339):
    """挑一个没被占用的调试端口。9339 常被你自己的 Chrome 或旧进程占着。"""
    for p in [preferred] + list(range(9340, 9360)):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def find_browser():
    """返回浏览器可执行文件路径。显式设置 SG_BROWSER 环境变量可覆盖。"""
    env = os.environ.get("SG_BROWSER")
    if env and os.path.isfile(env):
        return env

    if sys.platform.startswith("win"):
        roots = [
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
        ]
        names = [
            ("Google\\Chrome\\Application\\chrome.exe", "Microsoft\\Edge\\Application\\msedge.exe"),
        ]
        cands = []
        for r in roots:
            if not r:
                continue
            for pair in names:
                for n in pair:
                    cands.append(os.path.join(r, n))
        cands += [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
    elif sys.platform == "darwin":
        cands = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        cands = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/microsoft-edge",
        ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def count_segments(url, referer, timeout=12):
    """拉播放列表数分片；-1 表示拿不到。"""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    if referer:
        req.add_header("Referer", referer)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore").count("#EXTINF")
    except Exception:
        return -1


def origin_of(url):
    m = re.match(r"(https?://[^/]+)/", url)
    return m.group(1) + "/" if m else ""


def _kill(proc):
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def resolve(page_url, proxy=None, max_wait=55, log=print):
    """返回 (m3u8 地址, referer)；失败返回 (None, None)。"""
    try:
        import websocket
    except ImportError:
        log("缺少依赖 websocket-client，请执行：pip install -r requirements.txt")
        return None, None

    browser = find_browser()
    if not browser:
        log("没找到 Chrome / Edge。装一个，或用环境变量 SG_BROWSER 指定路径。")
        return None, None

    referer = origin_of(page_url)
    profile = tempfile.mkdtemp(prefix="sgrab_profile_")
    port = free_port()

    args = [browser,
            "--user-data-dir=" + profile,
            "--remote-debugging-port=%d" % port,
            "--no-first-run", "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--autoplay-policy=no-user-gesture-required",
            "--mute-audio",
            "--window-size=880,620", "--window-position=120,80",
            "about:blank"]
    if proxy:
        args.insert(1, "--proxy-server=" + proxy)
        log("浏览器经代理打开页面：%s" % proxy)
    else:
        log("未使用代理，直接打开页面")

    kw = {}
    if not sys.platform.startswith("win"):
        kw["start_new_session"] = True
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)

    ws_url = None
    for _ in range(40):
        time.sleep(0.5)
        try:
            d = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/json/list" % port, timeout=2).read())
            pages = [t for t in d if t.get("type") == "page"]
            if pages:
                ws_url = pages[0]["webSocketDebuggerUrl"]
                break
        except Exception:
            pass

    if not ws_url:
        log("连不上浏览器调试端口，可能被安全软件拦截。")
        _kill(proc)
        shutil.rmtree(profile, ignore_errors=True)
        return None, None

    ws = websocket.create_connection(ws_url, timeout=20)
    mid = [0]
    hits = []

    def call(method, params=None, timeout=10):
        mid[0] += 1
        i = mid[0]
        ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        end = time.time() + timeout
        ws.settimeout(1.0)
        while time.time() < end:
            try:
                msg = json.loads(ws.recv())
            except Exception:
                continue
            if msg.get("method") == "Network.requestWillBeSent":
                u = msg["params"]["request"]["url"]
                if ".m3u8" in u and u not in hits:
                    hits.append(u)
            if msg.get("id") == i:
                return msg.get("result", {})
        return {}

    call("Network.enable")
    call("Page.navigate", {"url": page_url})
    log("页面已打开，等待播放器加载（有验证和贴片广告，通常 20-40 秒）")

    # 自动点播放：既点 video 元素，也点常见的覆盖层播放按钮
    play_js = ("(function(){"
               "document.querySelectorAll('video').forEach(function(v){"
               "v.muted=true;if(v.paused){var p=v.play();if(p&&p.catch)p.catch(function(){});}});"
               "var sel=['.plyr__control--overlaid','.vjs-big-play-button','.play-btn',"
               "'[class*=play] button','button[aria-label*=lay]'];"
               "sel.forEach(function(s){var b=document.querySelector(s);if(b)b.click();});"
               "return 1;})()")

    best, best_n = None, 0
    deadline = time.time() + max_wait
    last_click = 0
    while time.time() < deadline:
        ws.settimeout(1.0)
        try:
            msg = json.loads(ws.recv())
            if msg.get("method") == "Network.requestWillBeSent":
                u = msg["params"]["request"]["url"]
                if ".m3u8" in u and u not in hits:
                    hits.append(u)
        except Exception:
            pass

        if time.time() - last_click > 5:
            last_click = time.time()
            call("Runtime.evaluate", {"expression": play_js, "returnByValue": True}, timeout=4)
            for u in hits:
                n = count_segments(u, referer)
                if n > best_n:
                    best_n, best = n, u
            if best_n > 300:  # 已经是正片规模，不用再等
                break

    try:
        ws.close()
    except Exception:
        pass
    _kill(proc)
    time.sleep(0.8)
    shutil.rmtree(profile, ignore_errors=True)

    if hits:
        log("捕获到 %d 条播放列表：" % len(hits))
        for u in hits:
            n = count_segments(u, referer)
            log("  %5s 分片  %s%s" % (n, u[:88], "  <- 选中" if u == best else ""))
    return best, referer
