# -*- coding: utf-8 -*-
"""把一条播放地址变成 SiteProfile（机制档案）。

档案只描述机制，不含站点名：
是否 master、有哪些档位、多少分片、是否加密、是否带签名、是否需要 Referer。
后续选路与下载都读这份档案，不再判断“这是什么站”。
"""
import queue
import threading
import urllib.parse

from . import net, playlist

SIGN_STRONG = {"sign", "signature", "token", "auth_key", "whip", "expires", "expire"}
SIGN_WEAK = {"t", "e", "st", "ts", "time", "nonce", "rand"}
MEDIA_EXT = (".mp4", ".m4v", ".webm", ".mkv", ".mov", ".flv", ".m4a", ".mp3")


def is_media_file(url):
    """是不是单个媒体文件（而不是播放列表）。

    接口下发的直链常带预签名参数，扩展名在 query 之前，按 path 判断即可。
    """
    try:
        p = urllib.parse.urlparse(url).path.lower()
    except Exception:
        return False
    return p.endswith(MEDIA_EXT)


def query_keys(url):
    try:
        q = urllib.parse.urlparse(url).query
        return {k.lower() for k, _ in urllib.parse.parse_qsl(q, keep_blank_values=True)}
    except Exception:
        return set()


def is_signed(url):
    keys = query_keys(url)
    return bool(keys & SIGN_STRONG) or len(keys & SIGN_WEAK) >= 2


def looks_ip_bound(url):
    """签名里是否夹带客户端 IP 参数（whip / ip / client_ip 之类）。"""
    keys = query_keys(url)
    return bool(keys & {"whip", "ip", "cip", "client_ip", "clientip", "remote_ip"})


def origin_of(url):
    p = urllib.parse.urlparse(url)
    return "%s://%s/" % (p.scheme, p.netloc) if p.netloc else ""


def _playlist_get(url, referer, ua, proxy, cookies, tries=2):
    """拉播放列表。

    慢 CDN 上 57 KB 的列表可能要十几秒（实测 5–12 秒），单次 12 秒超时
    会把本来能拿到的列表判成失败，所以给足时间并重试一次。
    """
    last = (None, "")
    for _ in range(tries):
        code, text, _h = net.get(url, referer, ua, proxy, cookies,
                                 timeout=25, max_bytes=800000)
        if text and "#EXTM3U" in text:
            return code, text
        last = (code, text)
    return last


def _first_playlist(url, referer, ua, cookies, routes):
    """在候选路径上并发拉列表，谁先成功用谁。

    串行试的问题在慢 CDN 上很直观：直连要十几秒，失败后再试代理又是十几秒。
    列表请求可以重复，并发取先到的那份即可。
    """
    if len(routes) <= 1:
        proxy = routes[0] if routes else None
        code, text = _playlist_get(url, referer, ua, proxy, cookies)
        return (proxy, text) if text else (None, "")

    q = queue.Queue()
    stop = threading.Event()

    def worker(proxy):
        try:
            code, text = _playlist_get(url, referer, ua, proxy, cookies)
        except Exception:
            text = ""
        if text and not stop.is_set():
            stop.set()
            q.put((proxy, text))

    for p in routes:
        t = threading.Thread(target=worker, args=(p,), daemon=True)
        t.start()

    try:
        return q.get(timeout=28)
    except queue.Empty:
        return None, ""


def build(url, referer=None, ua=None, cookies=None, cfg=None, proxies=None):
    """探测并返回 SiteProfile。

    proxies 是候选代理列表（可为 [None]），并发尝试；命中的那条记在 fetch_route。
    """
    profile = {
        "url": url,
        "kind": "unknown",
        "master": False,
        "variants": [],
        "segment_count": -1,
        "key": "none",
        "fmp4": False,
        "signed": is_signed(url),
        "ip_bound": looks_ip_bound(url),
        "cross_domain": False,
        "referer_required": False,
        "referer": referer or "",
        "page_origin": origin_of(referer) if referer else origin_of(url),
        "fetch_route": None,
        "error": "",
    }

    tried = proxies if proxies is not None else [None]
    last = (None, "")

    # 单个媒体文件：不拉正文，直接当直链处理（拉 600KB 视频去嗅探没有意义）
    if is_media_file(url):
        profile["kind"] = "direct"
        profile["segment_base"] = url
        for proxy in tried:
            ok, got, code, why = net.probe_first_chunk(
                url, referer, ua, proxy, cookies, need=16384)
            if ok:
                profile["fetch_route"] = proxy
                profile["segment_base"] = url
                return profile
            last = (code, why)
        profile["error"] = ("直链探测失败：HTTP %s" % last[0]) if last[0] else (
            "直链探测失败：%s" % last[1])
        return profile

    used, body = _first_playlist(url, referer, ua, cookies, tried)
    if not body:
        profile["error"] = "探测失败：所有路径都没拿到播放列表"
        return profile
    profile["fetch_route"] = used

    kind = playlist.kind_of(body)
    profile["kind"] = kind

    if kind == "hls":
        parsed = playlist.parse_m3u8(body, url)
        profile["master"] = parsed["master"]
        media_url, media_text = url, body
        if parsed["master"]:
            profile["variants"] = parsed["variants"]
            # 下钻最高档，拿分片数与加密信息
            if parsed["variants"]:
                media_url = parsed["variants"][0]["url"]
                code, text = _playlist_get(media_url, referer, ua,
                                           profile["fetch_route"], cookies)
                if text:
                    media_text = text
                    sub = playlist.parse_m3u8(text, media_url)
                    profile["segment_count"] = len(sub["segments"])
                    profile["key"] = sub["key"]
                    profile["fmp4"] = sub["fmp4"]
                    profile["segment_base"] = sub["segments"][0] if sub["segments"] else ""
        else:
            profile["segment_count"] = len(parsed["segments"])
            profile["key"] = parsed["key"]
            profile["fmp4"] = parsed["fmp4"]
            profile["segment_base"] = parsed["segments"][0] if parsed["segments"] else ""

        # 浏览器取流要在本地重建 m3u8，需要逐条时长、密钥与初始段
        detail = playlist.media_detail(media_text, media_url)
        profile["media_url"] = media_url
        profile["segments"] = [e["uri"] for e in detail["entries"]]
        profile["durations"] = [e["dur"] for e in detail["entries"]]
        profile["key_method"] = detail["key_method"]
        profile["key_uri"] = detail["key_uri"]
        profile["key_iv"] = detail["key_iv"]
        profile["media_sequence"] = detail["media_sequence"]
        profile["target_duration"] = detail["target_duration"]
        profile["map_uri"] = detail["map_uri"]
        profile["byterange"] = detail["byterange"]

    seg = profile.get("segment_base") or ""
    if seg:
        p_host = urllib.parse.urlparse(seg).netloc
        u_host = urllib.parse.urlparse(url).netloc
        profile["cross_domain"] = bool(p_host and u_host and p_host != u_host)

    if profile["key"] == "sample-aes":
        profile["kind"] = "drm"
        profile["error"] = "检测到 SAMPLE-AES（DRM），不做处理"

    return profile
