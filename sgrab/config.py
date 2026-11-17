# -*- coding: utf-8 -*-
"""配置加载与外部工具定位。"""
import json
import os
import socket
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = ".exe" if os.name == "nt" else ""

DEFAULTS = {
    "tools_dir": "tools",
    "out_dir": "downloads",
    "threads": 16,
    "retries": 10,
    "fragment_retries": 30,
    "socket_timeout": 20,
    "proxy": "auto",
    "download_via_proxy": "auto",
    "cdp_port": 9222,
    "browser_profile": "",
    "server_port": 8765,
    "referer_map": {},
}

# 常见本地代理端口，按命中概率排序
PROXY_PORTS = (7897, 7890, 7891, 7892, 10808, 10809, 1080, 2080, 6152, 8889)


def _abs(p):
    if not p:
        return p
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def load(path=None):
    """读取 config.json，缺失项用默认值补齐。"""
    cfg = dict(DEFAULTS)
    p = path or os.path.join(ROOT, "config.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if not k.startswith("_"):
                    cfg[k] = v
        except Exception as e:
            print("[配置] 读取 %s 失败，使用默认值：%s" % (p, e))
    cfg["tools_dir"] = _abs(cfg["tools_dir"])
    cfg["out_dir"] = _abs(cfg["out_dir"])
    return cfg


def port_open(port, host="127.0.0.1", timeout=0.35):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def profile_dir(cfg):
    """专用浏览器配置目录。

    Chrome 从 157 起不允许在默认配置目录上开 DevTools 端口
    （会直接拒绝并打印 "requires a non-default data directory"），
    所以不能借用你日常那个配置，只能用一份独立的、登录一次就长期有效的。
    默认放在仓库下的 .chrome-profile，和 chrome-debug.bat 指向同一个目录。
    """
    p = (cfg or {}).get("browser_profile") or ""
    if p:
        return _abs(p)
    return os.path.join(ROOT, ".chrome-profile")


def find_debug_port(preferred=9222, extra=(9223, 9333, 9224)):
    """找一个已经开着 DevTools 端口的浏览器，返回端口号，没有则 None。

    只探端口通不通不够 —— 7897 也通，但那是代理。再问一句 /json/version，
    对面回的是 Chromium 才算数。
    """
    cands = []
    for p in (preferred,) + tuple(extra):
        if p and int(p) not in cands:
            cands.append(int(p))
    for p in cands:
        if not port_open(p):
            continue
        try:
            url = "http://127.0.0.1:%d/json/version" % p
            with urllib.request.urlopen(url, timeout=1.5) as r:
                d = json.loads(r.read().decode("utf-8", "replace") or "{}")
            if d.get("webSocketDebuggerUrl") or "Browser" in d:
                return p
        except Exception:
            continue
    return None


def detect_proxy():
    """返回形如 'http://127.0.0.1:7897' 的代理地址，没有则 None。"""
    for p in PROXY_PORTS:
        if port_open(p):
            return "http://127.0.0.1:%d" % p
    return None


def resolve_proxy(cfg):
    v = cfg.get("proxy", "auto")
    if v in (None, False, "none", "off"):
        return None
    if v == "auto":
        return detect_proxy()
    if isinstance(v, int):
        return "http://127.0.0.1:%d" % v
    v = str(v)
    if v.isdigit():
        return "http://127.0.0.1:" + v
    return v


PROXY_OFF = (False, None, 0, "none", "off", "false", "no")
PROXY_ON = (True, 1, "true", "on", "yes")


def proxy_mode(cfg):
    """download_via_proxy 的取值：off / on / auto（直连优先）/ smart（测速择优）。"""
    v = cfg.get("download_via_proxy", "auto")
    if v in PROXY_OFF:
        return "off"
    if v in PROXY_ON:
        return "on"
    if str(v).lower() in ("smart", "fastest", "speed"):
        return "smart"
    return "auto"


def download_proxy(cfg, proxy=None):
    """强制走代理时返回代理地址，否则 None。

    auto 不在这里决定：要按实测吞吐选路，见 sgrab/route.py。
    """
    if proxy_mode(cfg) != "on":
        return None
    return proxy or resolve_proxy(cfg)


def probe_proxy(cfg):
    """拉播放列表用的代理。列表只有几百字节，稳妥优先，off 时才强制直连。"""
    if proxy_mode(cfg) == "off":
        return None
    return resolve_proxy(cfg)


def _first_file(cands):
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def find_tool(cfg, name):
    """在 tools_dir、PATH、常见位置里找可执行文件。"""
    td = cfg["tools_dir"]
    local = [
        os.path.join(td, name + EXE),
        os.path.join(td, name, name + EXE),
        os.path.join(td, name, "bin", name + EXE),
    ]
    hit = _first_file(local)
    if hit:
        return hit
    w = shutil_which(name)
    if w:
        return w
    return None


def shutil_which(name):
    from shutil import which
    return which(name)


def find_ytdlp(cfg):
    return find_tool(cfg, "yt-dlp")


def find_ffmpeg(cfg):
    exe = find_tool(cfg, "ffmpeg")
    if not exe:
        return None
    # yt-dlp 的 --ffmpeg-location 可接受目录或文件
    return os.path.dirname(exe)


def ensure_out(cfg):
    os.makedirs(cfg["out_dir"], exist_ok=True)
    return cfg["out_dir"]


def setup_console():
    """Windows 控制台偶发 GBK 编码错误，统一成 UTF-8，并开启行缓冲避免与子进程进度条交错。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass
