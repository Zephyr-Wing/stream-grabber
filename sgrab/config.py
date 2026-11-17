# -*- coding: utf-8 -*-
"""配置加载与外部工具定位。"""
import json
import os
import socket
import sys

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
    "download_via_proxy": False,
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
