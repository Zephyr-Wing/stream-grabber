# -*- coding: utf-8 -*-
"""用真实浏览器把页面里的视频流地址「嗅」出来。

为什么不能直接用 HTTP 请求页面：
  1. 这类站点普遍挂了 Cloudflare 人机验证，命令行拿到的只是挑战页；
  2. 播放地址由 JS 动态下发，页面源码里没有静态链接；
  3. 播放器先放一段贴片广告，真正的正片要等播放器初始化后才请求。

做法：起一个浏览器，通过 DevTools 协议监听网络请求，自动点击播放，再挑出
「分片最多」的那条播放列表 —— 正片通常上千个分片，广告只有几十个。

三种启动方式：
  temp   —— 独立临时配置，不碰你的日常配置，但也没有登录态
  login  —— 复用你日常的浏览器配置，画质按你的账号来（需要先关掉那个浏览器）
  attach —— 连到一个已经开着调试端口的浏览器，不用关，还能顺手把登录态导出
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
import urllib.parse
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


def _user_data_dirs():
    """常见浏览器的「用户数据」根目录，按优先级排。"""
    out = []
    if sys.platform.startswith("win"):
        la = os.environ.get("LOCALAPPDATA", "")
        if la:
            out.append(os.path.join(la, "Google", "Chrome", "User Data"))
            out.append(os.path.join(la, "Microsoft", "Edge", "User Data"))
    elif sys.platform == "darwin":
        h = os.path.expanduser("~")
        out.append(os.path.join(h, "Library", "Application Support", "Google", "Chrome"))
        out.append(os.path.join(h, "Library", "Application Support", "Microsoft Edge"))
    else:
        h = os.path.expanduser("~")
        out.append(os.path.join(h, ".config", "google-chrome"))
        out.append(os.path.join(h, ".config", "microsoft-edge"))
    return [d for d in out if os.path.isdir(d)]


def real_profile():
    """找日常用的那份配置，返回 (user_data_dir, 配置名)。

    配置名不是硬编码 Default —— 多数人装过第二个浏览器会叫 Profile 1，
    真正在用的那个记在 Local State 的 profile.last_used 里。
    """
    for udd in _user_data_dirs():
        name = "Default"
        try:
            with open(os.path.join(udd, "Local State"), "r", encoding="utf-8") as f:
                last = (json.load(f).get("profile") or {}).get("last_used")
            if last and os.path.isdir(os.path.join(udd, last)):
                name = last
        except Exception:
            pass
        if os.path.isdir(os.path.join(udd, name)):
            return udd, name
    return None, None


def browser_running():
    """日常浏览器是否开着。开着就没法用它的配置启动第二个实例。"""
    try:
        if sys.platform.startswith("win"):
            out = subprocess.run(["tasklist"], capture_output=True).stdout.decode(
                "mbcs", "ignore").lower()
            return ("chrome.exe" in out) or ("msedge.exe" in out)
        names = ["Google Chrome", "chrome", "msedge", "chromium"]
        for n in names:
            if subprocess.run(["pgrep", "-f", n], capture_output=True).returncode == 0:
                return True
    except Exception:
        return False
    return False


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


def _http_json(url, method="GET", timeout=3):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def write_cookie_jar(cookies, path):
    """把 CDP 拿到的 cookie 写成 Netscape 格式，喂给 yt-dlp --cookies。

    比让 yt-dlp 自己去读浏览器的库可靠：那个库在浏览器运行时是独占的，
    而这里的 cookie 是从已登录的会话里直接读出来的明文。
    """
    lines = ["# Netscape HTTP Cookie File", "# from stream-grabber", ""]
    n = 0
    for c in cookies:
        name = c.get("name") or ""
        domain = c.get("domain") or ""
        if not name or not domain:
            continue
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        try:
            exp = c.get("expires")
            exp = int(float(exp)) if exp and float(exp) > 0 else 0
        except Exception:
            exp = 0
        dom = ("#HttpOnly_" + domain) if c.get("httpOnly") else domain
        lines.append("\t".join([dom, flag, c.get("path") or "/", secure,
                                str(exp), name, c.get("value") or ""]))
        n += 1
    if not n:
        return None
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return path
    except Exception:
        return None


def resolve(page_url, proxy=None, max_wait=55, log=print,
            profile=None, cdp_port=None, cookie_jar=None):
    """解析页面里的 m3u8。

    profile   : None 用临时配置；"default" 复用日常配置（要求浏览器已关闭）
    cdp_port  : 连一个已经开着调试端口的浏览器，不新起进程
    cookie_jar: 给了路径就把登录态写进去，供下载阶段使用

    返回 (m3u8, referer, cookie文件)；失败返回 (None, None, None)。
    """
    try:
        import websocket
    except ImportError:
        log("缺少依赖 websocket-client，请执行：pip install -r requirements.txt")
        return None, None, None

    referer = origin_of(page_url)
    proc, profile_dir, tab_id = None, None, None
    attached = bool(cdp_port)

    if attached:
        # 附着模式：不新起浏览器，借别人的一只已经登录的会话
        try:
            _http_json("http://127.0.0.1:%d/json/version" % cdp_port)
        except Exception:
            log("连不上 127.0.0.1:%d 的调试端口。确认浏览器是用 "
                "--remote-debugging-port=%d 启动的。" % (cdp_port, cdp_port))
            return None, None, None
        try:
            tab = _http_json("http://127.0.0.1:%d/json/new?%s"
                             % (cdp_port, urllib.parse.quote(page_url, safe="")),
                             method="PUT")
            ws_url, tab_id = tab.get("webSocketDebuggerUrl"), tab.get("id")
        except Exception as e:
            log("开新标签页失败：%s" % e)
            return None, None, None
        if not ws_url:
            log("调试端口没返回可用的标签页。")
            return None, None, None
        port = cdp_port
        log("附着到已开的浏览器（端口 %d），沿用它的登录态" % cdp_port)
    else:
        browser = find_browser()
        if not browser:
            log("没找到 Chrome / Edge。装一个，或用环境变量 SG_BROWSER 指定路径。")
            return None, None, None

        extra = []
        if profile == "default":
            udd, pname = real_profile()
            if not udd:
                log("找不到日常浏览器的配置目录，改用临时配置（不会有登录态）。")
                profile_dir = tempfile.mkdtemp(prefix="sgrab_profile_")
            else:
                if browser_running():
                    log("你的浏览器正在运行，没法用它的配置再起一个实例。")
                    log("  办法一：先关掉浏览器，再跑一次")
                    log("  办法二：不关，用 --cdp-port 连一个带调试端口的实例")
                    return None, None, None
                profile_dir = udd
                extra = ["--profile-directory=" + pname]
                log("使用你日常的浏览器配置（%s / %s），画质按账号来" % (udd, pname))
        else:
            profile_dir = tempfile.mkdtemp(prefix="sgrab_profile_")

        port = free_port()
        args = [browser,
                "--user-data-dir=" + profile_dir,
                "--remote-debugging-port=%d" % port,
                "--no-first-run", "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--autoplay-policy=no-user-gesture-required",
                "--mute-audio",
                "--window-size=880,620", "--window-position=120,80"] + extra + ["about:blank"]
        if proxy:
            args.insert(1, "--proxy-server=" + proxy)
            log("浏览器经代理打开页面：%s" % proxy)
        else:
            log("未使用代理，直接打开页面")

        kw = {}
        if not sys.platform.startswith("win"):
            kw["start_new_session"] = True
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **kw)

        ws_url = None
        for _ in range(40):
            time.sleep(0.5)
            try:
                d = _http_json("http://127.0.0.1:%d/json/list" % port, timeout=2)
                pages = [t for t in d if t.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass

        if not ws_url:
            log("连不上浏览器调试端口，可能被安全软件拦截。")
            _kill(proc)
            if profile != "default":
                shutil.rmtree(profile_dir, ignore_errors=True)
            return None, None, None

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

    # 顺手把登录态导出来，下载阶段要用同一份 cookie 才能拿到同样的画质
    jar = None
    if cookie_jar:
        cookies = []
        for method in ("Storage.getCookies", "Network.getAllCookies"):
            r = call(method, {}, timeout=8)
            if r and r.get("cookies"):
                cookies = r["cookies"]
                break
        jar = write_cookie_jar(cookies, cookie_jar)
        if jar:
            log("导出登录态 %d 条，下载会带着它" % len(cookies))

    try:
        ws.close()
    except Exception:
        pass

    if attached:
        # 只是借用了别人一只标签页，关掉标签页就行，别动浏览器
        try:
            _http_json("http://127.0.0.1:%d/json/close/%s" % (port, tab_id))
        except Exception:
            pass
    else:
        _kill(proc)
        time.sleep(0.8)
        if profile != "default":
            shutil.rmtree(profile_dir, ignore_errors=True)

    if hits:
        log("捕获到 %d 条播放列表：" % len(hits))
        for u in hits:
            n = count_segments(u, referer)
            log("  %5s 分片  %s%s" % (n, u[:88], "  <- 选中" if u == best else ""))
    return best, referer, jar
