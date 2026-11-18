# -*- coding: utf-8 -*-
"""配置与外部工具定位。"""
import json
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = ".exe" if os.name == "nt" else ""

DEFAULTS = {
    "out_dir": "downloads",
    "tools_dir": "tools",
    "threads": 16,
    "retries": 10,
    "fragment_retries": 30,
    "socket_timeout": 20,
    "proxy": "auto",
    "route_mode": "auto",      # auto / direct / proxy
    "server_port": 8765,
    "format_pref": "bv*[vcodec^=avc1]+ba/bv*+ba/b",
}

# 常见本地代理端口，按命中概率排序
PROXY_PORTS = (7897, 7890, 7891, 7892, 10808, 10809, 1080, 2080, 6152, 8889)

CONFIG_PATH = os.path.join(ROOT, "config.json")


def _abs(p):
    if not p:
        return p
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def load():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    if not k.startswith("_"):
                        cfg[k] = v
        except Exception as e:
            print("[配置] 读取失败，使用默认值：%s" % e)
    cfg["out_dir"] = _abs(cfg["out_dir"])
    cfg["tools_dir"] = _abs(cfg["tools_dir"])
    return cfg


def save(cfg):
    data = {k: v for k, v in cfg.items()}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
    td = cfg["tools_dir"]
    hit = _first_file([
        os.path.join(td, name + EXE),
        os.path.join(td, name, name + EXE),
        os.path.join(td, name, "bin", name + EXE),
    ])
    if hit:
        return hit
    from shutil import which
    return which(name)


def find_ytdlp(cfg):
    return find_tool(cfg, "yt-dlp")


def find_ffmpeg(cfg):
    exe = find_tool(cfg, "ffmpeg")
    return os.path.dirname(exe) if exe else None


def ensure_out(cfg, override=None):
    d = _abs(override) if override else cfg["out_dir"]
    os.makedirs(d, exist_ok=True)
    return d


def setup_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass
