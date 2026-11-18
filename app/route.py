# -*- coding: utf-8 -*-
"""选路：直连 / 代理。

判据只有一条：**这个分片能不能真的取到**（HTTP 200 + 收到非 HTML 的字节）。
能取到就用直连，取不到才用代理。

不拿单连接测速当判据。分片 CDN 普遍按连接限速，单条实测 141 KB/s 的地址，
16 线程并发实测能到 3.2 MB/s；用单连接速率比较两条路会得出相反结论。
真正的快慢在下载过程中由 jobs 的看门狗判断，那才是用户实际拿到的速度。
"""
import threading
import time
import urllib.parse

from . import config, net

# 只用于确认可达，不求样本充分
PROBE_BYTES = 32768
PROBE_SECONDS = 4
PROBE_TIMEOUT = 10
# 同一 CDN 的判定结果复用一段时间，避免每次下载都重新探测
CACHE_TTL = 600

_CACHE = {}
_CACHE_LOCK = threading.Lock()


def order(cfg, proxy):
    mode = (cfg.get("route_mode") or "auto").lower()
    if mode == "direct" or not proxy:
        return [None]
    if mode == "proxy":
        return [proxy]
    return [None, proxy]


def _kb(rate):
    return "%.0f KB/s" % (rate / 1024.0) if rate < 1048576 else "%.1f MB/s" % (
        rate / 1048576.0)


def _sample_all(seg, referer, ua, cookies, routes):
    """并发探测所有候选路径，返回 {proxy_or_None: (ok, rate)}。"""
    out = {}
    lock = threading.Lock()

    def one(p):
        try:
            ok, _got, _code, _why, rate = net.sample(
                seg, referer, ua, p, cookies,
                need=PROBE_BYTES, max_seconds=PROBE_SECONDS,
                timeout=PROBE_TIMEOUT)
        except Exception:
            ok, rate = False, 0.0
        with lock:
            out[p] = (ok, rate)

    if len(routes) == 1:
        one(routes[0])
        return out

    threads = [threading.Thread(target=one, args=(p,), daemon=True) for p in routes]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=PROBE_TIMEOUT + 4)
    return out


def choose(cfg, profile, ua=None, cookies=None, proxy=None):
    """返回 dict(route, proxy, note) 或 None（表示两条路都不通）。"""
    seg = profile.get("segment_base") or ""
    if not seg:
        p = None if (cfg.get("route_mode") == "direct") else proxy
        return {"route": "proxy" if p else "direct", "proxy": p,
                "note": "无分片可探，按模式默认"}

    referer = profile.get("page_origin") or profile.get("referer") or ""
    routes = order(cfg, proxy)

    key = (urllib.parse.urlparse(seg).netloc,
           (cfg.get("route_mode") or "auto").lower(), bool(proxy))
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        p = hit[1]
        return {"route": "proxy" if p else "direct", "proxy": p,
                "note": "沿用上次判定：" + hit[2]}

    res = _sample_all(seg, referer, ua, cookies, routes)
    d = res.get(None)
    p = res.get(proxy) if proxy else None
    d_ok, d_rate = d if d else (False, 0.0)
    p_ok, p_rate = p if p else (False, 0.0)

    if d_ok:
        picked = {"route": "direct", "proxy": None,
                  "note": "直连可用" + ("（首片 %s）" % _kb(d_rate) if d_rate else "")}
    elif p_ok:
        picked = {"route": "proxy", "proxy": proxy,
                  "note": "直连取不到，改用代理"}
    else:
        return None

    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), picked["proxy"], picked["note"])
    return picked
